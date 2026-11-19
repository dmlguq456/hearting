#!/usr/bin/env python3
"""The route form: one judgment for "does one owner process run this route alone", and the one table
of which intensities a form may carry (design 3a, behaviour unchanged).

Every route form the repository's fixtures build is compiled for real, then the judgments are held
against the answer the code gave before: the intensity (`effective_intensity == "quick"`). Two judgments
are read from the route's nodes: A (one owner makes the route's work) and B (the owner starts depth-2
nodes). The middle cell `solo·standard` (A and B both true) and the team route that `--shape solo` with a
signal sealed before the cell opened (shape `solo`, team nodes: A false, B true) are held beside them.
"""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import importlib.util
import json
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, HERE / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


U = _load("unit_route_launch_for_route_form", "unit_route_launch.test.py")
F, R, T = U.F, U.R, U.T
import route_form as RF  # noqa: E402
import route_plan as RP  # noqa: E402
import capability_route_compose as COMPOSE  # noqa: E402


def old_answer(route):
    """What every site answered before: the intensity."""
    return route["effective_intensity"] == "quick"


class FormFixtures(U.UnitLegBase):
    """One real route of every form the fixtures build."""

    def compose(self, **kw):
        arguments = dict(capability="autopilot-code", capability_mode="dev", shape="solo", graph=None,
                         slug="route-form-fixture", cwd=R.ROOT, artifact_root=self.root, spec_read="fixture",
                         campaign_key="form-key", registered_headless_evidence=T.registered_headless(),
                         dispatch_evidence={"tuples": [T.nested("claude", "codex")]},
                         work_request={"text": "Change one thing", "owner_harness": None})
        arguments.update(kw)
        return R.compose_route(**arguments)

    def preset(self, capability, mode, intensity, **kw):
        arguments = dict(predicates=[], transport=None if intensity == "quick" else "headless",
                         tracking="tracked", tracked_gate_evidence=T.gate_evidence(),
                         registered_headless_evidence=T.registered_headless() if intensity == "quick" else None,
                         dispatch_evidence=None if intensity == "quick" else {"tuples": [T.nested("claude", "codex")]})
        arguments.update(kw)
        return R.compile_route(capability, mode, intensity, R.ROOT, R.ROOT, **arguments)

    def routes(self):
        """(label, route, expected form, is the intensity quick, has an owner process)."""
        direct = self.compose(shape="direct", slug="rf-direct")
        solo = self.compose(shape="solo", slug="rf-solo")
        resume = self.compose(capability="autopilot-lab", capability_mode="setup", shape="solo",
                              graph="resume-run,run-verify", slug="rf-resume")
        unit = self.unit_route(slug="rf-unit")[0]
        framed = self.compose(shape="framed", capability=None, capability_mode=None, slug="rf-framed")
        legacy = json.loads(json.dumps(solo))
        legacy["selection"].pop("shape")
        framed_one_leg = self.compose(shape="framed", capability=None, capability_mode=None, intensity="quick",
                                      slug="rf-framed-one-leg")
        self.assertEqual([n["id"] for n in framed_one_leg["nodes"]], ["frame", "route-decision"])
        rows = [
            ("direct", direct, "direct", False),
            ("unit", unit, "unit", True),
            ("solo quick (compose)", solo, "solo", True),
            ("preset quick (frame pair)", self.preset("autopilot-code", "dev", "quick"), "solo", True),
            ("resource resume", resume, "solo", True),
            ("staged standard", self.preset("autopilot-code", "dev", "standard"), "staged", False),
            ("staged strong", self.preset("autopilot-code", "dev", "strong",
                                          signals=["shared-contract"]), "staged", False),
            ("staged (compose)", self.compose(shape="staged", graph="execute,test,report", slug="rf-staged"),
             "staged", False),
            ("framed", framed, "framed", False),
            ("transactional-owner", self.preset("autopilot-refine", "default", "standard"), "staged", False),
            ("older record without selection.shape", legacy, "solo", True),
        ]
        rows.append(("framed (one frame leg)", framed_one_leg, "framed", False))
        return rows

    def solo_signal_team_route(self):
        """The route `compose --shape solo --signal <signal>` sealed before the middle cell opened:
        `selection.shape: solo`, `effective_intensity: standard`, the kind's whole team recipe for nodes.
        Rebuilt from a team route (a signal raises the intensity, not the form) with the shape recorded
        as `solo` and the seal recomputed, exactly the bytes an older release wrote."""
        route = json.loads(json.dumps(self.compose(shape="staged", graph=None, slug="rf-solo-signal-team",
                                                   signals=["shared-contract"])))
        route["selection"]["shape"] = "solo"
        route["route_hash"] = R.route_hash({k: v for k, v in route.items() if k not in ("route_hash", "route_id")})
        route["route_id"] = R.ROUTE_IDENTITY.route_id_from_hash(route["route_hash"])
        return route

    def middle_cell_route(self):
        return self.compose(shape="solo", signals=["shared-contract"], slug="rf-middle-cell")


class SameAnswerTest(FormFixtures):
    def test_the_new_judgments_answer_what_the_intensity_answered_in_every_route_form(self):
        seen = {}
        for label, route, form, quick in self.routes():
            with self.subTest(label):
                self.assertEqual(old_answer(route), quick)
                self.assertEqual(RF.route_form(route), form)
                # a route run by serial attempts from a candidate list: exactly the old "quick" answer
                self.assertEqual(RF.serial_attempt_route(route), old_answer(route))
                # one owner process alone: the old answer, except the unit route that has no owner
                self.assertEqual(RF.one_shot_owner(route), old_answer(route) and route["capability"] != "unit")
                self.assertEqual(RF.one_shot_owner(route),
                                 any(n.get("id") == "one-shot" and n.get("unit") == "_kernel/owner"
                                     for n in route["nodes"]) and old_answer(route))
                # stage nodes at depth 2: exactly the old "not direct, not quick" answer
                self.assertEqual(RF.staged_form(RF.route_form(route)),
                                 route["effective_intensity"] not in ("direct", "quick"))
                # B, the owner starts depth-2 nodes: the old "not direct, not quick" answer, except the
                # owner-only routes (no depth-2 node: nothing to start)
                self.assertEqual(RF.starts_depth2_nodes(route),
                                 route["effective_intensity"] not in ("direct", "quick"))
                # a route of one owner and no stage nodes ends alone (direct, unit, solo·quick)
                self.assertEqual(RF.alone_without_stages(route), route["effective_intensity"] in ("direct", "quick"))
                self.assertFalse(RF.one_shot_review_route(route))
                self.assertIsNone(RF.one_shot_review_node(route))
                seen[label] = RF.one_shot_owner(route)
        self.assertEqual({k: v for k, v in seen.items() if v and k.startswith(("unit", "direct", "staged"))}, {})
        self.assertTrue(seen["solo quick (compose)"] and seen["preset quick (frame pair)"] and seen["resource resume"])

    def test_the_forms_that_look_like_one_shot_but_are_not(self):
        rows = {label: route for label, route, _form, _quick in self.routes()}
        unit = rows["unit"]
        self.assertEqual((unit["effective_intensity"], [n["id"] for n in unit["nodes"]]), ("quick", ["unit"]))
        self.assertFalse(RF.one_shot_owner(unit))
        self.assertTrue(RF.serial_attempt_route(unit))
        pair = rows["preset quick (frame pair)"]
        self.assertEqual([n["id"] for n in pair["nodes"]], ["frame", "frame-alternative", "one-shot"])
        self.assertTrue(RF.one_shot_owner(pair))
        resume = rows["resource resume"]
        self.assertEqual([n["id"] for n in resume["nodes"]], ["resume-run", "one-shot"])
        self.assertTrue(RF.one_shot_owner(resume))
        owner_standard = rows["transactional-owner"]
        self.assertEqual(owner_standard["effective_intensity"], "standard")
        self.assertEqual(owner_standard["nodes"][-1]["kind"], "capability-owner")
        self.assertFalse(RF.one_shot_owner(owner_standard))

    def test_a_shape_the_intensity_contradicts_reads_as_the_intensity_always_did(self):
        solo = self.compose(shape="solo", slug="rf-contradict")
        for shape, intensity, form in (("staged", "quick", "solo"),
                                       ("direct", "quick", "solo"), ("framed", "quick", "solo"),
                                       (None, "quick", "solo"), (None, "direct", "direct"),
                                       (None, "strong", "staged")):
            with self.subTest(shape=shape, intensity=intensity):
                route = json.loads(json.dumps(solo))
                route["effective_intensity"] = intensity
                route["selection"]["shape"] = shape
                self.assertEqual(RF.route_form(route), form)
        # `solo` recorded on a route whose nodes are a team's (what `--shape solo --signal` sealed): a team route
        team = self.solo_signal_team_route()
        self.assertEqual((team["selection"]["shape"], team["effective_intensity"]), ("solo", "standard"))
        self.assertEqual(RF.route_form(team), "staged")
        # a non-dict record names no form, and is no owner, no stage route and no route one process ends alone
        for junk in (None, [], "route"):
            self.assertIsNone(RF.route_form(junk))
            self.assertFalse(RF.one_shot_owner(junk) or RF.starts_depth2_nodes(junk) or RF.alone_without_stages(junk)
                             or RF.serial_attempt_route(junk) or RF.one_shot_review_route(junk))

    def test_the_team_route_a_solo_shape_with_a_signal_sealed_stays_a_team_route(self):
        """Review of 3a, 🟡 1: widening the table must not turn this already sealed route into a solo owner's."""
        team = self.solo_signal_team_route()
        self.assertEqual([n["id"] for n in team["nodes"]],
                         ["plan", "plan-check", "execute", "impl-review", "test", "report"])
        self.assertFalse(RF.one_shot_owner(team))
        self.assertFalse(RF.serial_attempt_route(team))
        self.assertTrue(RF.starts_depth2_nodes(team))
        self.assertFalse(RF.one_shot_review_route(team))
        self.assertFalse(RF.alone_without_stages(team))
        # and it still verifies as the team route it is (no `quick route shape mismatch`)
        R.verify_route(json.loads(json.dumps(team)), team["cwd"])

    def test_the_middle_cell_is_an_owner_that_starts_a_review(self):
        cell = self.middle_cell_route()
        self.assertEqual([n["id"] for n in cell["nodes"]], ["impl-review", "one-shot"])
        self.assertTrue(RF.one_shot_owner(cell))                    # A
        self.assertTrue(RF.starts_depth2_nodes(cell))               # B
        self.assertTrue(RF.serial_attempt_route(cell))
        self.assertTrue(RF.one_shot_review_route(cell))
        self.assertEqual(RF.one_shot_review_node(cell)["id"], "impl-review")
        self.assertEqual(RF.route_form(cell), "solo")
        self.assertFalse(RF.staged_form(RF.route_form(cell)))
        self.assertFalse(RF.alone_without_stages(cell))
        self.assertNotEqual(cell["effective_intensity"], "quick")


class AllowanceTableTest(unittest.TestCase):
    def test_the_values_are_the_ones_the_five_places_held(self):
        self.assertEqual(RF.FORM_INTENSITIES["direct"], ("direct",))
        self.assertEqual(RF.FORM_INTENSITIES["unit"], ("quick",))
        # `solo` carries `standard` only where its kind declares a one-shot review (`allowed_intensities`)
        self.assertEqual(RF.FORM_INTENSITIES["solo"], ("quick", "standard"))
        self.assertEqual(RF.allowed_intensities("solo"), ("quick",))
        self.assertEqual(RF.allowed_intensities("solo", True), ("quick", "standard"))
        self.assertEqual(RF.FORM_INTENSITIES["staged"], ("standard", "strong", "thorough", "adversarial"))
        self.assertEqual(RF.SHAPE_INTENSITY, {"direct": "direct", "solo": "quick", "staged": "standard",
                                              "framed": "standard"})
        self.assertIs(RP.SHAPE_INTENSITY, RF.SHAPE_INTENSITY)
        self.assertIs(COMPOSE.SHAPE_INTENSITY, RF.SHAPE_INTENSITY)
        self.assertEqual(RF.INTENSITY_SHAPE, {"direct": "direct", "quick": "solo"})

    def test_the_refusal_text_is_the_text_compose_always_gave(self):
        self.assertEqual(RF.intensity_mismatch("direct", "standard"),
                         "compose-shape-intensity-mismatch:direct (allowed: direct)")
        self.assertEqual(RF.intensity_mismatch("solo", "standard"),
                         "compose-shape-intensity-mismatch:solo (allowed: quick)")
        self.assertEqual(RF.intensity_mismatch("solo", "strong", True),
                         "compose-shape-intensity-mismatch:solo (allowed: quick,standard)")
        self.assertIsNone(RF.intensity_mismatch("solo", "standard", True))
        self.assertEqual(RF.intensity_mismatch("staged", "quick"),
                         "compose-shape-intensity-mismatch:staged (allowed: standard,strong,thorough,adversarial)")
        self.assertEqual(RF.intensity_mismatch("staged", "direct"),
                         "compose-shape-intensity-mismatch:staged (allowed: standard,strong,thorough,adversarial)")
        for shape, intensity in (("direct", "direct"), ("solo", "quick"), ("staged", "standard"),
                                 ("staged", "adversarial"), ("framed", "quick")):
            self.assertIsNone(RF.intensity_mismatch(shape, intensity))

    def test_solo_opens_standard_only_for_a_kind_that_declares_the_review(self):
        for intensity in ("standard", "strong", "thorough", "adversarial", "direct"):
            self.assertIsNotNone(RF.intensity_mismatch("solo", intensity))
        for intensity in ("strong", "thorough", "adversarial", "direct"):
            self.assertIsNotNone(RF.intensity_mismatch("solo", intensity, True))
        self.assertEqual(RP.one_shot_review_declared("autopilot-code"), True)
        self.assertEqual(RP.one_shot_review_declared("autopilot-lab"), False)


class FrameProposalNormalizationTest(unittest.TestCase):
    def leg(self, shape, intensity, capability="autopilot-code"):
        notes = []
        row = RP._normal_leg({"capability": capability, "shape": shape, "intensity": intensity,
                              "why": "w"}, 0, notes)
        return row, notes

    def test_an_intensity_a_form_does_not_allow_keeps_the_highest_it_does_below_it_with_the_same_note(self):
        # code declares the one-shot review, so `solo` allows quick and standard
        cases = {("solo", "standard"): ("standard", False), ("solo", "strong"): ("standard", True),
                 ("solo", "adversarial"): ("standard", True), ("solo", "quick"): ("quick", False),
                 ("direct", "quick"): ("direct", True), ("staged", "quick"): ("standard", True),
                 ("staged", "strong"): ("strong", False), ("staged", "standard"): ("standard", False)}
        for (shape, intensity), (kept, noted) in cases.items():
            with self.subTest(shape=shape, intensity=intensity):
                row, notes = self.leg(shape, intensity)
                self.assertEqual(row["intensity"], kept)
                self.assertEqual(bool(notes), noted)
                if noted:
                    self.assertEqual(notes, [f"ignored:legs[0].intensity:{intensity}-for-{shape}"])

    def test_a_kind_that_declares_no_review_keeps_solo_at_quick(self):
        for intensity in ("standard", "strong"):
            row, notes = self.leg("solo", intensity, capability="autopilot-lab")
            self.assertEqual((row["intensity"], notes), ("quick", [f"ignored:legs[0].intensity:{intensity}-for-solo"]))


if __name__ == "__main__":
    unittest.main()
