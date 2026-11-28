#!/usr/bin/env python3
"""`solo` at `strong`, `thorough` and `adversarial` (route form S2): one owner makes the work and starts the review
legs its kind declares, with the one start command its task gives; the cells below it stay as they were.

Real compose and verify. The legs are the registry's own implementation-review group (the one a `staged` route
realizes); the anchor takes the cell's review model so `strong` never reviews more shallowly than `standard`.
The independent test node that follows the legs (S3) has its own tests in `route_solo_test_node.test.py`.
"""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, HERE / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


CELL = _load("route_middle_cell_for_solo_intensity", "route_middle_cell.test.py")
FORM = CELL.FORM
R, T = FORM.R, FORM.T
import route_form as RF  # noqa: E402
import route_plan as RP  # noqa: E402
import route_authority as AUTH  # noqa: E402
import worker_bootstrap as W  # noqa: E402
import dispatch_terminal_commit as DTC  # noqa: E402
import dispatch_completion_join as JOIN  # noqa: E402
import capability_route_close as CLOSE  # noqa: E402
import work_start as WS  # noqa: E402

LEGS = {  # intensity: the review nodes of the cell, as (id, model, perspective)
    "strong": (("impl-review", "deep", "primary-code-review"),
               ("impl-review-alternative", "light", "independent-code-review")),
    "thorough": (("impl-review", "deep", "primary-code-review"),
                 ("impl-review-alternative", "light", "independent-code-review"),
                 ("impl-review-failure-mode", "deep", "failure-mode-review")),
}
LEGS["adversarial"] = LEGS["thorough"]
CAP = {"strong": 2, "thorough": 3, "adversarial": 3}
HOPS = ["same-harness-headless", "cross-harness-headless"]

EXPECTED_SIGNATURES = {   # label: ((intensity, shape, topology, owner profile, max depth, review round cap), "id:model:depends_on ...")
    'direct': (('direct', 'direct', 'inline', None, 0, 1),
        'inline:light:-'),
    'docs staged': (('standard', 'solo', 'staged', 'deep', 2, 2),
        'frame:top:- frame-alternative:top:- material-strategy:balanced-deep:frame+frame-alternative '
        'material-strategy-alternative:light:frame+frame-alternative '
        'strategy-review:light:material-strategy+material-strategy-alternative '
        'draft-production:balanced-deep:strategy-review quality-review:light:draft-production '
        'fact-verify:light:draft-production finalize:balanced-deep:quality-review+fact-verify'),
    'framed': (('standard', 'framed', 'staged', 'deep', 1, 2),
        'frame:top:- frame-alternative:top:- route-decision:-:frame+frame-alternative'),
    'lab solo quick': (('quick', 'solo', 'one-shot-owner', 'balanced-deep', 1, 1),
        'one-shot:balanced-deep:-'),
    'preset quick': (('quick', 'solo', 'one-shot-owner', 'balanced', 1, 1),
        'frame:top:- frame-alternative:top:- one-shot:balanced:frame+frame-alternative'),
    'preset standard': (('standard', 'staged', 'staged', 'deep', 2, 2),
        'frame:top:- frame-alternative:top:- plan:balanced-deep:frame+frame-alternative plan-check:light:plan '
        'execute:balanced:plan-check impl-review:light:execute test:light:impl-review '
        'report:balanced-deep:test'),
    'preset strong': (('strong', 'staged', 'staged', 'deep', 2, 2),
        'frame:top:- frame-alternative:top:- plan:deep:frame+frame-alternative '
        'plan-alternative:balanced-deep:frame+frame-alternative plan-check:light:plan+plan-alternative '
        'plan-check-alternative:balanced-deep:plan+plan-alternative '
        'execute:balanced:plan-check+plan-check-alternative impl-review:balanced-deep:execute '
        'impl-review-alternative:light:execute test:light:impl-review+impl-review-alternative '
        'report:balanced-deep:test'),
    'preset thorough': (('thorough', 'staged', 'staged', 'deep', 2, 3),
        'frame:top:- frame-alternative:top:- plan:deep:frame+frame-alternative '
        'plan-alternative:balanced-deep:frame+frame-alternative '
        'plan-implementation-risk:light:frame+frame-alternative '
        'plan-check:light:plan+plan-alternative+plan-implementation-risk '
        'plan-check-alternative:balanced-deep:plan+plan-alternative+plan-implementation-risk '
        'plan-check-simplicity:light:plan+plan-alternative+plan-implementation-risk '
        'execute:balanced:plan-check+plan-check-alternative+plan-check-simplicity '
        'impl-review:balanced-deep:execute impl-review-alternative:light:execute '
        'impl-review-failure-mode:deep:execute '
        'test:light:impl-review+impl-review-alternative+impl-review-failure-mode report:balanced-deep:test'),
    'solo quick': (('quick', 'solo', 'one-shot-owner', 'balanced', 1, 1),
        'one-shot:balanced:-'),
    'solo standard explicit': (('standard', 'solo', 'one-shot-owner', 'balanced', 2, 2),
        'impl-review:deep:- one-shot:balanced:impl-review'),
    'solo standard profile light': (('standard', 'solo', 'one-shot-owner', 'light', 2, 2),
        'impl-review:light:- one-shot:light:impl-review'),
    'solo standard signal': (('standard', 'solo', 'one-shot-owner', 'balanced', 2, 2),
        'impl-review:deep:- one-shot:balanced:impl-review'),
    'staged adversarial': (('adversarial', 'staged', 'staged', 'deep', 2, 3),
        'plan:deep:- plan-alternative:balanced-deep:- plan-implementation-risk:light:- '
        'plan-check:light:plan+plan-alternative+plan-implementation-risk '
        'plan-check-alternative:balanced-deep:plan+plan-alternative+plan-implementation-risk '
        'plan-check-simplicity:light:plan+plan-alternative+plan-implementation-risk '
        'execute:balanced:plan-check+plan-check-alternative+plan-check-simplicity '
        'impl-review:balanced-deep:execute impl-review-alternative:light:execute '
        'impl-review-failure-mode:deep:execute '
        'test:light:impl-review+impl-review-alternative+impl-review-failure-mode report:balanced-deep:test'),
    'staged graph': (('standard', 'staged', 'staged', 'deep', 2, 2),
        'execute:balanced:- test:light:execute report:balanced-deep:test'),
    'staged standard': (('standard', 'staged', 'staged', 'deep', 2, 2),
        'plan:balanced-deep:- plan-check:light:plan execute:balanced:plan-check impl-review:light:execute '
        'test:light:impl-review report:balanced-deep:test'),
    'staged strong': (('strong', 'staged', 'staged', 'deep', 2, 2),
        'plan:deep:- plan-alternative:balanced-deep:- plan-check:light:plan+plan-alternative '
        'plan-check-alternative:balanced-deep:plan+plan-alternative '
        'execute:balanced:plan-check+plan-check-alternative impl-review:balanced-deep:execute '
        'impl-review-alternative:light:execute test:light:impl-review+impl-review-alternative '
        'report:balanced-deep:test'),
    'staged thorough': (('thorough', 'staged', 'staged', 'deep', 2, 3),
        'plan:deep:- plan-alternative:balanced-deep:- plan-implementation-risk:light:- '
        'plan-check:light:plan+plan-alternative+plan-implementation-risk '
        'plan-check-alternative:balanced-deep:plan+plan-alternative+plan-implementation-risk '
        'plan-check-simplicity:light:plan+plan-alternative+plan-implementation-risk '
        'execute:balanced:plan-check+plan-check-alternative+plan-check-simplicity '
        'impl-review:balanced-deep:execute impl-review-alternative:light:execute '
        'impl-review-failure-mode:deep:execute '
        'test:light:impl-review+impl-review-alternative+impl-review-failure-mode report:balanced-deep:test'),
    'unit': (('quick', 'solo', 'one-shot-owner', 'balanced-deep', 1, 1),
        'unit:light:-'),
}


class Base(FORM.FormFixtures):
    def solo(self, intensity, **kw):
        return self.compose(shape="solo", intensity=intensity, slug="s2-" + intensity, **kw)

    def verify(self, route):
        return R.verify_route(json.loads(json.dumps(route)), route["cwd"])

    def ids(self, route):
        return [n["id"] for n in route["nodes"]]


class SoloIntensity(Base):

    # -- what the new cells are -------------------------------------------------------------------------
    def test_the_records_of_the_new_cells(self):
        for intensity, legs in LEGS.items():
            with self.subTest(intensity):
                route = self.solo(intensity)
                self.verify(route)
                self.assertEqual((route["effective_intensity"], route["selection"]["shape"], route["execution_topology"],
                                  route["owner_dispatch_depth"], route["max_dispatch_depth"],
                                  route["registered_headless_policy"], route["owner_model_profile"]),
                                 (intensity, "solo", "one-shot-owner", 1, 2, "serial-attempt", "balanced"))
                *reviews, test, owner = route["nodes"]
                self.assertEqual([(n["id"], n["model_profile"], n["perspective"]) for n in reviews], list(legs))
                self.assertEqual((test["id"], test["unit"], test["model_profile"], test["depends_on"], test["inputs"],
                                  [hop["fallback_hop"] for hop in test["fallback_hops"]]),
                                 ("test", "qa/test", "light", [n["id"] for n in reviews], ["source-diff"], HOPS))
                for review in reviews:
                    self.assertEqual((review["unit"], review["kind"], review["dispatch_depth"], review["depends_on"],
                                      review["completion_gate"], review["leg_class"], review["inputs"],
                                      review["parallel_group"], review["parallel_leg_count"]),
                                     ("qa/code-review", "review-worker", 2, [], "code-impl-review", "peer",
                                      ["source-diff"], "impl-review", len(legs)))
                    # a registered reviewer is the only one that can close a review of the owner's own work
                    self.assertEqual([hop["fallback_hop"] for hop in review["fallback_hops"]], HOPS)
                self.assertEqual((owner["id"], owner["unit"], owner["dispatch_depth"], owner["terminal"],
                                  sorted(owner["depends_on"])), ("one-shot", "_kernel/owner", 1, True,
                                                                  sorted(n["id"] for n in [*reviews, test])))
                self.assertFalse(owner.get("fallback_hops"))
                self.assertEqual(len({path for n in reviews for path in n["outputs"]}), len(reviews))   # disjoint reports
                self.assertEqual(route["continuation_budget"]["review_round_cap"], CAP[intensity])
                self.assertEqual(route["continuation_budget"]["declared_nodes"], len(legs) + 2)
                self.assertEqual(route["human_gates"], [])              # a decided shape has no frame pair
                self.assertTrue(RF.one_shot_owner(route) and RF.starts_depth2_nodes(route) and RF.one_shot_review_route(route))
                self.assertEqual(RF.one_shot_review_node_ids(route), [n["id"] for n in [*reviews, test]])
                self.assertEqual(RF.route_form(route), "solo")
                self.assertFalse(RF.alone_without_stages(route))

    def test_adversarial_is_the_thorough_nodes_with_its_own_rigor(self):
        thorough, adversarial = self.solo("thorough"), self.solo("adversarial")
        keep = lambda route: [(n["id"], n["unit"], n["model_profile"], n.get("perspective"), n["depends_on"])  # noqa: E731
                              for n in route["nodes"]]
        self.assertEqual(keep(thorough), keep(adversarial))
        self.assertEqual((thorough["effective_intensity"], adversarial["effective_intensity"]), ("thorough", "adversarial"))
        self.verify(adversarial)

    def test_the_legs_are_the_group_a_team_route_realizes_and_only_the_anchor_takes_the_cells_model(self):
        for intensity in ("strong", "thorough"):
            with self.subTest(intensity):
                solo = self.solo(intensity)
                team = self.compose(shape="staged", intensity=intensity, graph="execute,impl-review,test,report",
                                    slug="s2-team-" + intensity)
                mine = [n for n in solo["nodes"] if n.get("parallel_group")]
                theirs = [n for n in team["nodes"] if n.get("parallel_group") == "impl-review"]
                key = lambda n: (n["id"], n["perspective"], n["leg_class"], n["outputs"], n["write_scope"])  # noqa: E731
                self.assertEqual([key(n) for n in mine], [key(n) for n in theirs])
                # the group's declared models for the other legs; the anchor is the cell's `deep`, not the group's anchor
                self.assertEqual([n["model_profile"] for n in mine[1:]], [n["model_profile"] for n in theirs[1:]])
                self.assertEqual((mine[0]["model_profile"], theirs[0]["model_profile"]), ("deep", "balanced-deep"))

    def test_a_signal_and_an_explicit_intensity_meet_at_the_higher(self):
        route = self.compose(shape="solo", signals=["shared-contract"], intensity="strong", slug="s2-signal")
        self.assertEqual((route["effective_intensity"], self.ids(route)),
                         ("strong", ["impl-review", "impl-review-alternative", "test", "one-shot"]))
        self.verify(route)

    def test_a_profile_and_explicit_profiles_still_win(self):
        light = self.solo("strong", profile="light")
        self.assertEqual((light["owner_model_profile"], [n["model_profile"] for n in light["nodes"]]),
                         ("light", ["light", "light", "light", "light"]))
        self.verify(light)
        picked = self.solo("thorough", explicit_profiles={"impl-review-alternative": "deep"})
        self.assertEqual([n["model_profile"] for n in picked["nodes"]], ["deep", "deep", "deep", "light", "balanced"])
        self.verify(picked)

    def test_the_preview_and_the_compose_again_command_name_every_leg(self):
        for intensity, legs in LEGS.items():
            with self.subTest(intensity):
                preview = R.compose_route(
                    capability="autopilot-code", capability_mode="dev", shape="solo", graph=None, slug="preview",
                    cwd=R.ROOT, artifact_root=self.root, spec_read="fixture", campaign_key="form-key",
                    intensity=intensity, selection_only=True)
                self.assertEqual([n["id"] for n in preview["nodes"]], [leg[0] for leg in legs] + ["test", "one-shot"])
                route = self.solo(intensity)
                path = Path(self.root) / "route-autoclose-task"
                route = {**route, "artifact_root": str(self.root)}
                import shlex
                argv = shlex.split(WS._compose_again(route))
                flag = lambda name: argv[argv.index(name) + 1] if name in argv else None  # noqa: E731
                self.assertEqual((flag("--shape"), flag("--intensity")), ("solo", intensity))
                again = self.compose(shape="solo", intensity=flag("--intensity"), slug="s2-again-" + intensity)
                self.assertEqual(self.ids(again), self.ids(route))
                self.verify(again)

    # -- the cells that must stay as they were -----------------------------------------------------------
    def build_signature_cases(self):
        return {
            "solo quick": self.compose(shape="solo", slug="eq-q"),
            "solo standard explicit": self.compose(shape="solo", intensity="standard", slug="eq-s"),
            "solo standard signal": self.compose(shape="solo", signals=["shared-contract"], slug="eq-ss"),
            "solo standard profile light": self.compose(shape="solo", intensity="standard", profile="light", slug="eq-sl"),
            "direct": self.compose(shape="direct", slug="eq-d"),
            "staged graph": self.compose(shape="staged", graph="execute,test,report", slug="eq-sg"),
            "staged standard": self.compose(shape="staged", intensity="standard", slug="eq-st"),
            "staged strong": self.compose(shape="staged", intensity="strong", slug="eq-stg"),
            "staged thorough": self.compose(shape="staged", intensity="thorough", slug="eq-sth"),
            "staged adversarial": self.compose(shape="staged", intensity="adversarial", slug="eq-sad"),
            "preset quick": self.preset("autopilot-code", "dev", "quick"),
            "preset standard": self.preset("autopilot-code", "dev", "standard"),
            "preset strong": self.preset("autopilot-code", "dev", "strong", signals=["shared-contract"]),
            "preset thorough": self.preset("autopilot-code", "dev", "thorough", signals=["shared-contract"]),
            "lab solo quick": self.compose(capability="autopilot-lab", capability_mode="setup", shape="solo", slug="eq-lab"),
            "docs staged": self.compose(capability="autopilot-draft", capability_mode="doc", shape="solo",
                                        signals=["artifact-fanout"], slug="eq-docs"),
            "unit": self.unit_route(slug="eq-unit")[0],
            "framed": self.compose(shape="framed", capability=None, capability_mode=None, slug="eq-framed"),
        }

    def test_quick_standard_direct_unit_framed_and_every_team_cell_record_what_they_recorded(self):
        """Compare node profiles and dependencies, owner profile, depth and review rounds for each cell."""
        built = self.build_signature_cases()
        self.assertEqual(sorted(built), sorted(EXPECTED_SIGNATURES))
        for label, route in built.items():
            with self.subTest(label):
                head, nodes = EXPECTED_SIGNATURES[label]
                got = (route["effective_intensity"], (route.get("selection") or {}).get("shape"),
                       route.get("execution_topology"), route.get("owner_model_profile"),
                       route.get("max_dispatch_depth"), route["continuation_budget"]["review_round_cap"])
                self.assertEqual(got, head)
                self.assertEqual(" ".join(f"{n['id']}:{n.get('model_profile') or '-'}:{'+'.join(n.get('depends_on') or []) or '-'}"
                                          for n in route["nodes"]), nodes)
                self.verify(route)

    def test_the_standard_cell_borrows_exactly_the_one_review_node_it_always_did(self):
        recipe = R.TOPO.resolve_recipe(R.TOPO.load_registry(), "autopilot-code", "dev")
        self.assertEqual(R.one_shot_review_nodes(recipe, "standard"), [R.one_shot_review_node(recipe)])
        self.assertEqual(self.ids(self.compose(shape="solo", intensity="standard", slug="s2-std")),
                         ["impl-review", "one-shot"])
        self.assertEqual(self.ids(self.compose(shape="solo", slug="s2-quick")), ["one-shot"])

    # -- who may open them --------------------------------------------------------------------------------
    def test_a_kind_that_declares_no_review_keeps_its_refusal_byte_for_byte(self):
        for capability, mode in (("autopilot-lab", "setup"), ("autopilot-draft", "doc")):
            for intensity in ("standard", "strong", "thorough", "adversarial"):
                with self.subTest(capability=capability, intensity=intensity):
                    with self.assertRaises(ValueError) as caught:
                        self.compose(capability=capability, capability_mode=mode, shape="solo", intensity=intensity,
                                     slug="s2-nodecl")
                    self.assertEqual(str(caught.exception), "compose-shape-intensity-mismatch:solo (allowed: quick)")

    def test_the_other_refusals_name_the_new_values(self):
        with self.assertRaises(ValueError) as caught:
            self.compose(shape="solo", intensity="direct", slug="s2-direct")
        self.assertEqual(str(caught.exception),
                         "compose-shape-intensity-mismatch:solo (allowed: quick,standard,strong,thorough,adversarial)")
        with self.assertRaises(ValueError) as caught:
            self.compose(shape="staged", intensity="quick", slug="s2-staged-quick")
        self.assertEqual(str(caught.exception),
                         "compose-shape-intensity-mismatch:staged (allowed: standard,strong,thorough,adversarial)")

    def test_a_frame_proposal_keeps_solo_at_strong_and_up_where_the_kind_declares_the_review(self):
        for intensity in ("strong", "thorough", "adversarial"):
            notes = []
            row = RP._normal_leg({"capability": "autopilot-code", "shape": "solo", "intensity": intensity, "why": "w"}, 0, notes)
            self.assertEqual((row["intensity"], notes), (intensity, []))
            notes = []
            row = RP._normal_leg({"capability": "autopilot-lab", "shape": "solo", "intensity": intensity, "why": "w"}, 0, notes)
            self.assertEqual((row["intensity"], notes), ("quick", [f"ignored:legs[0].intensity:{intensity}-for-solo"]))

    # -- verify refuses a route that is not exactly the declared one ---------------------------------------------
    def test_verify_refuses_a_cell_that_differs_from_the_declaration(self):
        strong, thorough = self.solo("strong"), self.solo("thorough")
        copy = lambda route: json.loads(json.dumps(route))  # noqa: E731
        edits = {}
        no_alt = copy(strong)
        no_alt["nodes"] = [no_alt["nodes"][0], no_alt["nodes"][2]]
        no_alt["nodes"][1]["depends_on"] = ["impl-review"]
        edits["strong without its second leg"] = no_alt
        third = copy(strong)
        extra = copy(third["nodes"][1])
        extra["id"] = "impl-review-extra"
        third["nodes"].insert(2, extra)
        third["nodes"][3]["depends_on"] = ["impl-review", "impl-review-alternative", "impl-review-extra"]
        edits["strong with an undeclared third leg"] = third
        relabeled = copy(thorough)
        relabeled["effective_intensity"] = "strong"
        edits["thorough nodes sealed as strong"] = relabeled
        widened = copy(strong)
        widened["effective_intensity"] = "thorough"
        edits["strong nodes sealed as thorough"] = widened
        standard = copy(strong)
        standard["effective_intensity"] = "standard"
        edits["strong nodes sealed as standard"] = standard
        inline = copy(strong)
        inline["nodes"][1]["fallback_hops"].append({"ordinal": 4, "fallback_hop": "inline",
                                                    "status": "eligible-after-prior-hop-exhaustion",
                                                    "reason_enum": "runtime-unavailable", "fleet_visibility": "none"})
        edits["an inline hop on the second leg"] = inline
        partial_end = copy(strong)
        partial_end["nodes"][2]["depends_on"] = ["impl-review"]
        edits["an end that waits on one leg only"] = partial_end
        other_unit = copy(strong)
        other_unit["nodes"][1]["unit"] = "qa/test"
        edits["the second leg is another unit"] = other_unit
        swapped = copy(strong)
        swapped["nodes"][0], swapped["nodes"][1] = swapped["nodes"][1], swapped["nodes"][0]
        edits["the legs out of the declared order"] = swapped
        other_input = copy(strong)
        other_input["nodes"][1]["inputs"] = ["plan.md", "source-diff"]
        edits["a plan as the second leg's input"] = other_input
        for label, edited in edits.items():
            with self.subTest(label):
                with self.assertRaises(ValueError):
                    self.verify(CELL.reseal(edited))
        self.verify(strong)
        self.verify(thorough)

    def test_verify_refuses_a_review_leg_whose_model_was_changed_with_its_selection(self):
        # A leg the route chose no profile for runs the model its kind declares, whichever way the seal is redone.
        for intensity in ("standard", "strong", "thorough"):
            route = self.solo(intensity)
            for at, node in enumerate(route["nodes"][:-1]):
                self.assertEqual((node.get("profile_selection") or {}).get("source"), "legacy", node["id"])
                other = "light" if node["model_profile"] != "light" else "deep"
                with self.subTest(intensity=intensity, leg=node["id"]):
                    edited = json.loads(json.dumps(route))
                    edited["nodes"][at]["model_profile"] = other
                    edited["nodes"][at]["profile_selection"]["resolved_profile"] = other
                    with self.assertRaisesRegex(ValueError, "one-shot review model differs"):
                        self.verify(CELL.reseal(edited))
        # a profile the person chose is sealed as theirs and still verifies
        self.verify(self.solo("strong", profile="light"))
        self.verify(self.solo("standard", profile="light"))

    # -- the legs count their rounds on their own lineage; the owner closes none of them ------------------------------
    def decide(self, route, node, rows, digest, action="round"):
        return AUTH.decide_continuation(route, action=action, node=node, rows=rows,
                                        facts={"source_subject_digest": digest})

    def test_every_leg_counts_its_own_rounds_and_the_cap_follows_the_intensity(self):
        for intensity in ("strong", "thorough"):
            route = self.solo(intensity)
            cap = CAP[intensity]
            for node in [n for n in route["nodes"] if n.get("kind") == "review-worker"]:   # the test: S3's own file
                with self.subTest(intensity=intensity, leg=node["id"]):
                    rows = [CELL.row(CELL.BLOCKING, f"d{i}", f"att-{i}") for i in range(1, cap + 1)]
                    # after a blocking round the owner fixed (the digest moved): a closure check, not a first review
                    closure = self.decide(route, node, rows[:1], "d2")
                    self.assertEqual((closure.allowed, closure.budget.round_kind), (True, "closure-check"))
                    # the owner changed nothing: no round is spent
                    same = self.decide(route, node, rows[:1], "d1")
                    self.assertEqual((same.allowed, same.reason), (False, "review-subject-unchanged"))
                    # every round of the cap used and the owner fixed again: no more, whatever the digest
                    spent = self.decide(route, node, rows, f"d{cap + 1}")
                    self.assertEqual((spent.allowed, spent.state, spent.budget.verdict_rounds),
                                     (False, "exhausted", cap))
                    if cap == 3:   # thorough: the third round is still open after two blocking ones
                        third = self.decide(route, node, rows[:2], "d3")
                        self.assertEqual((third.allowed, third.budget.verdict_rounds), (True, 2))

    def test_a_leg_that_passed_is_judged_again_on_the_changed_source(self):
        route = self.solo("strong")
        anchor = route["nodes"][0]
        passed = ("done", {"attempt_id": "att-p", "note": CELL.PASSING, "failure_class": "pass", "worker_type": "review",
                           "review_subject_digest": "d1"})
        again = self.decide(route, anchor, [passed], "d2")
        self.assertEqual((again.allowed, again.budget.round_kind, again.budget.verdict_rounds), (True, "correction", 1))

    def test_the_owner_cannot_close_any_leg_of_its_own_work(self):
        for intensity in ("strong", "thorough"):
            route = self.solo(intensity)
            spent = [CELL.row(CELL.BLOCKING, "d1", "att-1"), CELL.row(CELL.BLOCKING, "d2", "att-2")]
            for node in route["nodes"][:-1]:
                for action in ("owner-closure", "complete-inline"):
                    with self.subTest(intensity=intensity, leg=node["id"], action=action):
                        refused = self.decide(route, node, spent, "d3", action=action)
                        self.assertEqual((refused.allowed, refused.reason), (False, action + "-review-of-own-work"))
                        self.assertFalse(self.decide(route, node, [], "d1", action=action).allowed)
        # a team conductor keeps both on its own group legs (unchanged)
        team = self.compose(shape="staged", intensity="strong", graph="execute,impl-review,test,report", slug="s2-team")
        leg = next(n for n in team["nodes"] if n["id"] == "impl-review-alternative")
        self.assertTrue(self.decide(team, leg, [], "d1", action="complete-inline").allowed)

    def test_the_legs_are_the_nodes_the_fallback_check_and_the_round_rules_read(self):
        route = self.solo("strong")
        self.assertEqual(RF.one_shot_review_node_ids(route), ["impl-review", "impl-review-alternative", "test"])
        team = self.compose(shape="staged", intensity="strong", graph="execute,impl-review,test,report", slug="s2-team-ids")
        self.assertEqual(RF.one_shot_review_node_ids(team), [])
        self.assertEqual(RF.one_shot_review_nodes(self.compose(shape="solo", slug="s2-q")), [])

    # -- the end waits on every leg ---------------------------------------------------------------------------------
    def end_gate(self, route, legs):
        """The terminal gate row with an owner marker on disk and each leg's marker as `legs` names it
        (`None`: passes; otherwise the reason it does not)."""
        jobs = Path(self.root) / "jobs.log"
        jobs.write_text("", encoding="utf-8")
        owner = route["nodes"][-1]
        marker = CLOSE.completion_dir(route["route_id"], jobs=jobs) / (owner["id"] + ".json")
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("{}", encoding="utf-8")

        def identity(_route, node, node_id, gate, **kw):
            reason = legs.get(node_id)
            if node_id == owner["id"] or reason is None:
                return {"passed": True, "reason": "completion-marker-verified"}
            return {"passed": False, "reason": reason}
        with mock.patch.object(CLOSE, "_marker_identity_row", side_effect=identity):
            return CLOSE.terminal_gate_observation(route, jobs=jobs)[owner["id"]]

    def test_the_end_is_proven_only_when_every_leg_passes_on_the_current_source(self):
        for intensity, legs in LEGS.items():
            route = self.solo(intensity)
            ids = [leg[0] for leg in legs]
            self.assertTrue(self.end_gate(route, {})["passed"])
            for missing in ids:
                for reason in ("completion-marker-absent", "completion-review-source-stale"):
                    with self.subTest(intensity=intensity, leg=missing, reason=reason):
                        row = self.end_gate(route, {missing: reason})
                        self.assertFalse(row["passed"], row)
                        self.assertTrue(row["reason"].startswith("owner-prerequisite-unproven:"), row)
                        self.assertIn(reason, row["reason"])


class SoloIntensityTexts(Base):
    """What the owner is told when its review is a group, and what stays unsaid for every other route."""

    def route_file(self, route, name="route.json"):
        path = Path(tempfile.mkdtemp(prefix="s2-")) / name
        path.write_text(json.dumps(route), encoding="utf-8")
        self.addCleanup(lambda: path.unlink(missing_ok=True))
        return path

    def owner_args(self, route):
        return SimpleNamespace(worker_type="owner", route_file=str(self.route_file(route)))

    def test_the_task_gives_the_owner_one_command_for_the_whole_group(self):
        for intensity, legs in LEGS.items():
            with self.subTest(intensity):
                args = self.owner_args(self.solo(intensity))
                text = W.owner_route_steps_prompt(args, {})
                command = W.route_start_command(args.route_file, "impl-review")
                self.assertIn(f"`{command}`", text)
                self.assertEqual(text.count("stage-dispatch-fallback.py"), 1)
                self.assertIn(f"{len(legs)} independent reviews of it", text)
                for leg in legs:
                    self.assertIn(leg[0], text)
                for phrase in ("runtime_wait: registered-children", "every blocking finding of every blocking report",
                               "start the closure checks with the same command", "return your final handoff",
                               "cannot review or test your own work", "do not run `complete --inline`",
                               f"Each review and the test get {CAP[intensity]} rounds in all", "end with `verdict: FAIL`"):
                    self.assertIn(phrase, text)
                self.assertEqual(W.owner_inline_marker_prompt(args), "")
                self.assertNotIn("conductor", text)

    def test_the_standard_cell_and_the_team_hear_what_they_always_heard(self):
        text = W.owner_route_steps_prompt(self.owner_args(self.compose(shape="solo", intensity="standard", slug="s2-txt-std")), {})
        self.assertIn("one independent review (impl-review, qa/code-review) of it", text)
        self.assertIn("The review gets 2 rounds in all: if the closure check blocks too", text)
        team = self.compose(shape="staged", intensity="strong", slug="s2-txt-team")
        self.assertNotIn("independent review", W.owner_route_steps_prompt(self.owner_args(team), {}))

    def test_several_blocking_reports_are_named_together_with_one_command(self):
        one = {"kind": "review", "attempt": "att-a", "artifact": "/c/a.md", "target": "", "source": True,
               "command": "python3 start-group", "producer_command": "", "owner_fixes": True, "closure_spent": False,
               "corrections": 1}
        two = {**one, "attempt": "att-b", "artifact": "/c/b.md"}
        text = W.continuation_followup_text([one, two])
        for part in ("att-a, att-b", "Fix every blocking finding in /c/a.md, /c/b.md in this worktree in one pass",
                     "start the closure checks with the same command: python3 start-group"):
            self.assertIn(part, text)
        self.assertEqual(text.count("python3 start-group"), 1)
        self.assertNotIn("producing stage", text)
        spent = W.continuation_followup_text([one, {**two, "closure_spent": True}])
        self.assertIn("Do not correct further", spent)
        self.assertIn("`verdict: FAIL`", spent)
        self.assertNotIn("python3 start-group", spent)
        # one report keeps the single text; the count of corrections follows the intensity
        self.assertEqual(W.continuation_followup_text([one]), W.continuation_followup_text(one))
        self.assertIn("the one correction allowed is spent", W.continuation_followup_text({**one, "closure_spent": True}))
        self.assertIn("the 2 corrections allowed are spent",
                      W.continuation_followup_text({**one, "closure_spent": True, "corrections": 2}))
        # a group's spent text says what is true of its legs and counts the corrections of its intensity
        self.assertIn("a review still blocks, and the correction allowed is spent",
                      W.continuation_followup_text([{**one, "closure_spent": True}, two]))
        self.assertIn("a review still blocks, and the 2 corrections allowed are spent",
                      W.continuation_followup_text([{**one, "closure_spent": True, "corrections": 2},
                                                    {**two, "corrections": 2}]))
        self.assertIn(": a review still blocks, and the one correction allowed is spent.",
                      W.continuation_followup_text({**one, "closure_spent": True, "group": True}))
        self.assertIn(": the closure check still blocks, and the one correction allowed is spent.",
                      W.continuation_followup_text({**one, "closure_spent": True}))   # one review: the bytes it was
        # the team text is the bytes it was
        team = {"kind": "review", "attempt": "att-r1", "artifact": "/c/report.md", "target": "", "source": True,
                "command": "python3 start-review", "producer_command": "python3 start-execute"}
        self.assertEqual(W.continuation_followup_text([team, {**team, "attempt": "att-r2"}]),
                         "Review FAIL: batch the findings in /c/report.md into one correction of the source "
                         "through the bounded producing stage. First use python3 start-execute. "
                         "Then run the same start for the closure check: python3 start-review")

    def wake(self, route, reason="completion-marker-absent", lineage=None, fields=None, reasons=None):
        path = self.route_file(route)
        row = SimpleNamespace(status="open", metadata={"attempt_id": "att-owner"})
        gaps = {n["id"]: reason for n in route["nodes"] if n.get("dispatch_depth") == 2}
        gaps.update(reasons or {})
        with mock.patch.object(JOIN, "exact_attempt_row", return_value=row), \
             mock.patch.object(DTC, "_completion_owner_choice", return_value=SimpleNamespace(allowed=True)), \
             mock.patch.object(DTC, "owner_workflow_gaps", return_value=gaps), \
             mock.patch.object(W, "_lineage_rows", return_value=lineage or {}), \
             mock.patch.object(W, "continuation_followup_fields",
                               side_effect=lambda _r, node, *a, **k: (fields or {}).get(node["id"], {})):
            return DTC.owner_workflow_continuation("/jobs.log", "att-owner", str(path)), path

    def test_the_wake_text_for_a_group_names_what_is_true_and_the_one_start_command(self):
        route = self.solo("strong")
        fields = {"kind": "review", "attempt": "att-1", "artifact": "/c/alt.md", "target": "", "source": True,
                  "command": "python3 start-group", "producer_command": "", "owner_fixes": True, "closure_spent": False,
                  "corrections": 1}
        blocked = CELL.row(CELL.BLOCKING, "d1", "att-1")
        passed = ("done", {"attempt_id": "att-p", "note": CELL.PASSING, "failure_class": "pass", "worker_type": "review",
                           "review_subject_digest": "d1"})
        dead = ("done", {"attempt_id": "att-d", "note": "dead-capacity", "failure_class": "capacity",
                         "worker_type": "review", "review_subject_digest": "d1"})
        unrun, path = self.wake(route)
        command = W.route_start_command(path, "impl-review", jobs="/jobs.log")
        self.assertTrue(unrun.endswith("\nYour route's independent review has not run. Commit your work, then start it "
                                       f"with exactly: `{command}` and yield runtime_wait: registered-children."
                                       " You cannot review your own work or close this review inline; do not report PASS without it."))
        self.assertEqual(unrun.count("stage-dispatch-fallback.py"), 1)   # one command for the whole group
        stale, _ = self.wake(route, reason="completion-review-source-stale",
                             lineage={"impl-review": [passed], "impl-review-alternative": [passed]})
        self.assertIn("passed, but the work changed after it", stale)
        self.assertNotIn("has not run", stale)
        # one leg's pass is stale while the other gave no verdict or has not run: both are said, not only the pass
        stale_reason = "completion-review-source-stale"
        for other in ({"impl-review-alternative": [dead]}, {}):
            mixed, _ = self.wake(route, lineage={"impl-review": [passed], **other},
                                 reasons={"impl-review": stale_reason})
            self.assertIn("Part of your route's independent review passed but the work changed after it, and the rest "
                          "ended without a verdict or has not run.", mixed)
            self.assertEqual(mixed.count("stage-dispatch-fallback.py"), 1)
        alone = self.wake(self.solo("standard"), reason=stale_reason)[0]       # one review: the bytes it was
        self.assertIn("Your route's independent review passed, but the work changed after it.", alone)
        again, _ = self.wake(route, lineage={"impl-review": [passed], "impl-review-alternative": [dead]})
        self.assertIn("ended without a verdict", again)
        # one leg blocks and the other passed: the blocking leg's own instruction, never "has not run"
        blocks, _ = self.wake(route, lineage={"impl-review": [passed], "impl-review-alternative": [blocked]},
                              fields={"impl-review-alternative": fields})
        self.assertTrue(blocks.endswith("\n" + W.continuation_followup_text(fields)))
        self.assertIn("Fix every blocking finding in /c/alt.md", blocks)
        self.assertNotIn("has not run", blocks)
        # both legs blocked: every report is named, the command is given once
        both, _ = self.wake(route, lineage={"impl-review": [blocked], "impl-review-alternative": [blocked]},
                            fields={"impl-review": {**fields, "attempt": "att-0", "artifact": "/c/anchor.md"},
                                    "impl-review-alternative": fields})
        self.assertIn("/c/anchor.md, /c/alt.md", both)
        self.assertEqual(both.count("python3 start-group"), 1)
        # a team route's text is the bytes it was
        team_text, _ = self.wake(self.preset("autopilot-code", "dev", "standard"))
        self.assertNotIn("stage-dispatch-fallback.py", team_text)
        self.assertTrue(team_text.endswith("Runtime settlement owns close/finalize."))

    def test_the_next_ready_line_of_a_group_does_not_contradict_a_blocking_leg(self):
        import dispatch_stage_advance as ADVANCE
        route = self.solo("strong")
        path = self.route_file(route)
        owner = route["nodes"][-1]
        metas = {}
        for node, note in (("impl-review", "completed-marker"), ("impl-review-alternative", "completed-review-blocking")):
            metas[node] = {"dispatch_depth": "2", "worker_type": "review", "route_file": str(path),
                           "route_id": route["route_id"], "route_hash": route["route_hash"], "note": note,
                           "route_node": node, "attempt_id": "att-" + node}
        receipt = {"children": [{"attempt_id": "att-impl-review"}, {"attempt_id": "att-impl-review-alternative"}]}
        with mock.patch.object(JOIN, "exact_attempt_row",
                               side_effect=lambda _jobs, attempt: SimpleNamespace(
                                   metadata=metas[attempt.removeprefix("att-")])), \
             mock.patch.object(ADVANCE, "completed_nodes", return_value=set()), \
             mock.patch.object(ADVANCE, "started_nodes", return_value=set()), \
             mock.patch.object(ADVANCE, "runnable_successors", return_value=[owner]):
            lines = JOIN._next_ready_start_lines(receipt, "/jobs.log")
            self.assertEqual(len(lines), 1, lines)
            self.assertIn("Follow the Review FAIL instruction above", lines[0])
            metas["impl-review-alternative"]["note"] = "completed-marker"
            lines = JOIN._next_ready_start_lines(receipt, "/jobs.log")
        self.assertEqual(lines, ["Next ready: nothing to start; the independent review has finished. "
                                 "Act on its verdict as your task describes, then return your final handoff."])



class SoloGroupRestart(Base):
    """Starting the review group again for a leg that gave no verdict does not judge the same bytes again
    for a leg that already passed on them: that leg's pass is used as it stands."""

    def setUp(self):
        super().setUp()
        import replica_batch_contract as CONTRACT
        from recovery_history import INPUT_SCHEMA, batch_input_path
        self.batch = _load("dispatch_batch_for_solo_group_restart", "dispatch-batch.py")
        self.route = self.solo("strong")
        self.nodes = [n for n in self.route["nodes"]
                      if n["id"] in RF.one_shot_review_node_ids(self.route) and n.get("parallel_group")]
        self.group = self.nodes[0]["parallel_group"]
        self.base = Path(self.root) / "restart"
        self.jobs = self.base / "dispatch" / "jobs.log"
        self.jobs.parent.mkdir(parents=True)
        self.prompt = "Review the current work."
        digest = "sha256:" + __import__("hashlib").sha256(self.prompt.encode()).hexdigest()
        self.attempts = ("att-anchor", "att-alternative")
        members = [{"assignment_sha256": digest, "attempt_id": attempt, "route_node": node["id"], "harness": harness,
                    "fallback_hop": "same-harness-headless", "fallback_ordinal": 1, "model_profile": node["model_profile"],
                    "perspective": node["perspective"], "parallel_leg_index": index, "leg_class": "peer"}
                   for index, (node, attempt, harness) in enumerate(zip(self.nodes, self.attempts, ("codex", "claude")))]
        manifest, manifest_digest, leg_digests = CONTRACT.build_manifest(
            parallel_group=self.group, route_id=self.route["route_id"], parent_attempt_id="att-owner",
            independence="cross-harness", members=members,
            required_independence_axes=["cross-harness", "model-profile", "perspective"],
            realized_independence_axes=["cross-harness", "model-profile", "perspective"])
        self.members, self.manifest_digest, self.leg_digests = members, manifest_digest, leg_digests
        input_path = batch_input_path(self.jobs, manifest_digest)
        input_path.parent.mkdir(parents=True, exist_ok=True)
        input_path.write_text(json.dumps({
            "schema": INPUT_SCHEMA, "jobs": str(self.jobs.resolve()), "route_id": self.route["route_id"],
            "route_hash": self.route["route_hash"], "manifest": manifest, "manifest_digest": manifest_digest,
            "options": {"parallel_group": self.group, "route": str(self.base / "route.json"), "parent": "owner",
                        "prompt_text": self.prompt}}, separators=(",", ":"), sort_keys=True), encoding="utf-8")
        self.state = self.base / "state"
        (self.state / "completion" / self.route["route_id"]).mkdir(parents=True)

    def rows(self, *outcomes):
        """One finished row per leg: `pass`, `blocking`, or `none` (ended without a verdict)."""
        lines = []
        for node, attempt, member, outcome in zip(self.nodes, self.attempts, self.members, outcomes):
            note, failure = {"pass": ("completed-marker", "pass"), "blocking": ("completed-review-blocking", "review"),
                             "none": ("dead-worker-fail", "fail")}[outcome]
            meta = {"attempt_schema_version": 2, "dispatch_depth": 2, "transport": "headless",
                    "execution_surface": "registered-headless", "registered_worker": "1", "attempt_id": attempt,
                    "route_id": self.route["route_id"], "route_hash": self.route["route_hash"], "route_node": node["id"],
                    "parent_attempt_id": "att-owner", "parent": "owner", "harness": member["harness"],
                    "child_harness": member["harness"], "fallback_hop": member["fallback_hop"], "fallback_ordinal": 1,
                    "reservation_kind": "parallel-batch", "batch_declared_size": len(self.nodes),
                    "parallel_group": self.group, "replica_group": self.group, "batch_group": self.group,
                    "batch_route_id": self.route["route_id"], "batch_parent_attempt_id": "att-owner",
                    "batch_attempt_id": attempt, "batch_route_node": node["id"], "batch_harness": member["harness"],
                    "batch_fallback_hop": member["fallback_hop"], "batch_fallback_ordinal": 1,
                    "batch_independence": "cross-harness", "batch_assignment_sha256": member["assignment_sha256"],
                    "batch_manifest_sha256": self.manifest_digest, "batch_leg_sha256": self.leg_digests[attempt],
                    "batch_model_profile": member["model_profile"], "batch_perspective": member["perspective"],
                    "batch_parallel_leg_index": member["parallel_leg_index"], "batch_leg_class": "peer",
                    "batch_auxiliary_check": "-", "note": note, "failure_class": failure, "launch_claimed": "1",
                    "launch_outcome": "reaped-before-publish"}
            lines.append("\t".join(["fixture", "done", str(self.base), str(self.base), attempt,
                                    ",".join(f"{k}={v}" for k, v in meta.items())]))
            (self.state / "completion" / self.route["route_id"] / f"{node['id']}.json").write_text(
                json.dumps({"route_id": self.route["route_id"], "node_id": node["id"], "attempt_id": attempt}),
                encoding="utf-8")
        self.jobs.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def peers(self, current=lambda attempt: True, route=None, nodes=None, prompt=None):
        """The legs a group start reuses; `current(attempt)` says whether that leg's marker describes the current source."""
        with mock.patch.object(self.batch, "dispatch_state_roots", return_value=[self.state]), \
                mock.patch.object(self.batch, "resolve_dispatch_state_root", return_value=self.state), \
                mock.patch.object(self.batch, "completion_marker_is_current",
                                  side_effect=lambda _r, _n, _p, marker, **_kw: current(marker["attempt_id"])), \
                mock.patch.object(self.batch, "completion_attempt_readiness",
                                  return_value=SimpleNamespace(state="ready", reason="settled")):
            return self.batch.current_completed_group_peers(
                self.jobs, route or self.route, nodes or self.nodes, self.group, self.base, prompt or self.prompt)

    def test_a_leg_that_passed_on_the_current_source_is_reused_and_owes_no_round(self):
        self.rows("pass", "none")
        peers = self.peers()
        self.assertEqual(set(peers), {"impl-review"})
        self.assertEqual(peers["impl-review"]["attempt_id"], "att-anchor")
        self.assertEqual([n["id"] for n in self.batch.pending_round_admission_nodes(self.nodes, peers)],
                         ["impl-review-alternative"])

    def test_every_leg_that_passed_is_reused_so_a_start_with_nothing_changed_launches_nothing(self):
        self.rows("pass", "pass")
        self.assertEqual(set(self.peers()), {"impl-review", "impl-review-alternative"})

    def test_a_pass_the_source_has_moved_past_is_not_used(self):
        self.rows("pass", "none")
        self.assertEqual(self.peers(current=lambda attempt: False), {})
        self.rows("pass", "pass")
        self.assertEqual(set(self.peers(current=lambda attempt: attempt == "att-alternative")), {"impl-review-alternative"})

    def test_a_blocking_leg_a_leg_without_a_verdict_and_another_request_are_not_reused(self):
        self.rows("blocking", "none")
        self.assertEqual(self.peers(), {})
        self.rows("pass", "pass")
        self.assertEqual(self.peers(prompt="a different review request"), {})

    def test_a_team_groups_pass_and_an_owner_closure_are_not_reused(self):
        self.rows("pass", "pass")
        team = self.compose(shape="staged", intensity="strong", graph="execute,impl-review,test,report", slug="s2-team-restart")
        team_nodes = [n for n in team["nodes"] if n["id"] in ("impl-review", "impl-review-alternative")]
        self.assertEqual(self.peers(route=team, nodes=team_nodes), {})
        (self.state / "completion" / self.route["route_id"] / "impl-review.json").write_text(json.dumps({
            "route_id": self.route["route_id"], "node_id": "impl-review", "attempt_id": "att-anchor",
            "review_gate_closure": "owner-closure", "review_independence": "owner-overridden"}), encoding="utf-8")
        self.assertEqual(set(self.peers()), {"impl-review-alternative"})


if __name__ == "__main__":
    unittest.main()
