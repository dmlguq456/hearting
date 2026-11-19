#!/usr/bin/env python3
"""The middle cell `solo·standard`: one owner makes the work and starts the kind's one declared review.

Real compose and verify. The cell is read from its nodes (A: one owner makes it, B: it starts a depth-2
node), never from the recorded shape, so the team route that `--shape solo` with a signal seals stays a
team route.
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


FORM = _load("route_form_for_middle_cell", "route_form.test.py")
U, F, R, T = FORM.U, FORM.F, FORM.R, FORM.T
import route_form as RF  # noqa: E402


import route_authority as AUTH  # noqa: E402
import review_round_cap as CAP  # noqa: E402
import worker_bootstrap as W  # noqa: E402
import dispatch_terminal_commit as DTC  # noqa: E402
import dispatch_owner_input as OWNER_INPUT  # noqa: E402
import artifact_lifecycle as LIFECYCLE  # noqa: E402
import capability_route_compose as COMPOSE  # noqa: E402


def reseal(route):
    """The route as a hand edit with a recomputed seal would look (what `verify_route` must still refuse)."""
    route = json.loads(json.dumps(route))
    route["route_hash"] = R.route_hash({k: v for k, v in route.items() if k not in ("route_hash", "route_id")})
    route["route_id"] = R.ROUTE_IDENTITY.route_id_from_hash(route["route_hash"])
    return route


def row(note, digest, attempt, failure_class="fail"):
    return ("done", {"attempt_id": attempt, "note": note, "failure_class": failure_class, "worker_type": "review",
                     "review_subject_digest": digest})


BLOCKING, PASSING = "completed-review-blocking", "completed-marker"


class Cell(FORM.FormFixtures):
    def cell(self, **kw):
        return self.compose(shape="solo", signals=["shared-contract"], slug="cell-fixture", **kw)

    def verify(self, route):
        return R.verify_route(json.loads(json.dumps(route)), route["cwd"])

    # -- what the cell is -------------------------------------------------------------------------
    def test_the_record_of_the_cell(self):
        route = self.cell()
        self.verify(route)
        self.assertEqual((route["effective_intensity"], route["selection"]["shape"], route["execution_topology"],
                          route["owner_dispatch_depth"], route["max_dispatch_depth"],
                          route["registered_headless_policy"]),
                         ("standard", "solo", "one-shot-owner", 1, 2, "serial-attempt"))
        review, owner = route["nodes"]
        self.assertEqual((review["id"], review["kind"], review["unit"], review["dispatch_depth"], review["depends_on"],
                          review["completion_gate"], review["model_profile"], review["inputs"]),
                         ("impl-review", "review-worker", "qa/code-review", 2, [], "code-impl-review", "deep",
                          ["source-diff"]))
        self.assertEqual((owner["id"], owner["unit"], owner["dispatch_depth"], owner["depends_on"], owner["terminal"],
                          owner["model_profile"], route["owner_model_profile"]),
                         ("one-shot", "_kernel/owner", 1, ["impl-review"], True, "balanced", "balanced"))
        # the review's chain is the declared hops: no `inline` (a maker cannot close its own review)
        self.assertEqual([hop["fallback_hop"] for hop in review["fallback_hops"]],
                         ["same-harness-headless", "cross-harness-headless", "native-subagent"])
        self.assertFalse(owner.get("fallback_hops"))
        # the owner keeps its candidates; the review's checked evidence is sealed beside them
        self.assertTrue(route["registered_headless_candidates"])
        self.assertTrue(route["dispatch_evidence"]["tuples"])
        self.assertEqual(route["human_gates"], [])           # compose `solo` has no frame pair
        self.assertEqual([n["id"] for n in route["nodes"] if "frame" in n["id"]], [])

    def test_a_signal_and_an_explicit_standard_open_the_same_cell(self):
        by_signal = self.cell()
        explicit = self.compose(shape="solo", intensity="standard", slug="cell-explicit")
        for route in (by_signal, explicit):
            self.assertEqual([n["id"] for n in route["nodes"]], ["impl-review", "one-shot"])
            self.assertEqual(route["effective_intensity"], "standard")
        self.verify(explicit)

    def test_the_commands_around_the_cell(self):
        quick = self.compose(shape="solo", slug="around-quick")
        self.assertEqual(([n["id"] for n in quick["nodes"]], quick["effective_intensity"], quick["max_dispatch_depth"],
                          quick["owner_model_profile"]), (["one-shot"], "quick", 1, "balanced-deep"))
        with self.assertRaisesRegex(ValueError, r"compose-shape-intensity-mismatch:solo \(allowed: quick,standard\)"):
            self.compose(shape="solo", intensity="strong", slug="around-strong")
        with self.assertRaisesRegex(ValueError, r"compose-shape-intensity-mismatch:solo \(allowed: quick\)"):
            self.compose(capability="autopilot-lab", capability_mode="setup", shape="solo", intensity="standard",
                         slug="around-lab")
        # a kind that declares no review: a signal still seals the team route it sealed before (shape recorded solo)
        draft = self.compose(capability="autopilot-draft", capability_mode="doc", shape="solo",
                             signals=["artifact-fanout"], slug="around-draft")
        self.assertEqual((draft["selection"]["shape"], draft["effective_intensity"], draft["execution_topology"]),
                         ("solo", "standard", "staged"))
        self.assertTrue(RF.starts_depth2_nodes(draft) and not RF.one_shot_owner(draft))
        self.verify(draft)
        staged = self.compose(shape="staged", graph="execute,test,report", slug="around-staged")
        self.assertEqual([n["id"] for n in staged["nodes"]], ["execute", "test", "report"])
        preset = self.preset("autopilot-code", "dev", "standard")
        self.assertEqual((preset["selection"]["shape"], preset["owner_model_profile"], len(preset["nodes"])),
                         ("staged", "deep", 8))
        self.assertFalse(RF.one_shot_owner(preset))

    def test_the_preview_shows_both_nodes(self):
        preview = R.compose_route(
            capability="autopilot-code", capability_mode="dev", shape="solo", graph=None, slug="preview",
            cwd=R.ROOT, artifact_root=self.root, spec_read="fixture", campaign_key="form-key",
            signals=["shared-contract"], selection_only=True)
        self.assertEqual([n["id"] for n in preview["nodes"]], ["impl-review", "one-shot"])
        quick = R.compose_route(capability="autopilot-code", capability_mode="dev", shape="solo", graph=None,
                                slug="preview", cwd=R.ROOT, artifact_root=self.root, spec_read="fixture",
                                campaign_key="form-key", selection_only=True)
        self.assertEqual([n["id"] for n in quick["nodes"]], ["one-shot"])

    def test_the_owner_model_role(self):
        self.assertEqual(self.cell()["owner_model_profile"], "balanced")
        # a profile, explicit profiles and a pin still win; the conductor's `deep` is not the maker's default
        light = self.cell(profile="light")
        self.assertEqual((light["owner_model_profile"], [n["model_profile"] for n in light["nodes"]]),
                         ("light", ["light", "light"]))
        self.verify(light)
        picked = self.cell(explicit_profiles={"__owner__": "deep"})
        self.assertEqual((picked["owner_model_profile"], picked["nodes"][1]["model_profile"],
                          picked["nodes"][0]["model_profile"]), ("deep", "deep", "deep"))
        self.verify(picked)
        self.assertEqual(self.preset("autopilot-code", "dev", "quick")["owner_model_profile"], "balanced-deep")

    # -- verify refuses a cell that is not exactly the declared one -------------------------------------
    def test_verify_refuses_a_cell_that_differs_from_the_declaration(self):
        route = self.cell()
        edits = {}
        inline = json.loads(json.dumps(route))
        inline["nodes"][0]["fallback_hops"].append({"ordinal": 4, "fallback_hop": "inline",
                                                    "status": "eligible-after-prior-hop-exhaustion",
                                                    "reason_enum": "runtime-unavailable", "fleet_visibility": "none"})
        edits["an inline hop on the review"] = inline
        no_review = json.loads(json.dumps(route))
        no_review["nodes"] = no_review["nodes"][1:]
        no_review["nodes"][0]["depends_on"] = []
        no_review["max_dispatch_depth"] = 1
        edits["no review node (a solo owner at standard)"] = no_review
        flat = json.loads(json.dumps(route))
        flat["max_dispatch_depth"] = 1
        edits["max_dispatch_depth 1"] = flat
        reverse = json.loads(json.dumps(route))
        reverse["nodes"][0]["depends_on"] = ["one-shot"]
        reverse["nodes"][1]["depends_on"] = []
        edits["the review waiting on the owner"] = reverse
        no_candidates = json.loads(json.dumps(route))
        no_candidates["registered_headless_candidates"] = None
        edits["no owner candidates"] = no_candidates
        no_evidence = json.loads(json.dumps(route))
        no_evidence["dispatch_evidence"] = None
        edits["no dispatch evidence for the review"] = no_evidence
        other_input = json.loads(json.dumps(route))
        other_input["nodes"][0]["inputs"] = ["plan.md", "source-diff"]
        edits["a plan the owner never writes as the review's input"] = other_input
        for label, edited in edits.items():
            with self.subTest(label):
                with self.assertRaises(ValueError):
                    self.verify(reseal(edited))
        self.verify(route)

    def test_a_standard_route_of_one_owner_node_is_not_the_cell(self):
        route = json.loads(json.dumps(self.compose(shape="solo", slug="not-the-cell")))
        route["effective_intensity"] = "standard"
        route["selection"]["shape"] = "solo"
        with self.assertRaises(ValueError):    # `solo` above quick is accepted only as exactly the declared cell
            self.verify(reseal(route))

    # -- the registry declaration ------------------------------------------------------------------------
    def test_the_registry_declares_the_cell_for_code_only(self):
        registry = R.TOPO.load_registry()
        declared = sorted({r["capability"] for r in registry["recipes"] if "one_shot_review" in r})
        self.assertEqual(declared, ["autopilot-code"])
        recipe = R.TOPO.resolve_recipe(registry, "autopilot-code", "dev")
        self.assertEqual(recipe["one_shot_review"], {
            "node": "impl-review", "inputs": ["source-diff"], "model_profile": "deep", "owner_model_profile": "balanced",
            "fallback_hops": ["same-harness-headless", "cross-harness-headless", "native-subagent"]})
        bad = json.loads(json.dumps(recipe))
        bad["one_shot_review"]["fallback_hops"].append("inline")
        with self.assertRaisesRegex(R.TOPO.TopologyError, "without inline"):
            R.TOPO._validate_recipe(bad, registry, registry["owner_profile_by_intensity"]["standard"])
        bad = json.loads(json.dumps(recipe))
        bad["one_shot_review"]["node"] = "execute"
        with self.assertRaisesRegex(R.TOPO.TopologyError, "review-worker"):
            R.TOPO._validate_recipe(bad, registry, registry["owner_profile_by_intensity"]["standard"])

    # -- the review's rounds, counted on its lineage ----------------------------------------------------
    def decide(self, route, node, rows, digest, action="round", **kw):
        return AUTH.decide_continuation(route, action=action, node=node, rows=rows,
                                        facts={"source_subject_digest": digest}, **kw)

    def test_the_cells_review_counts_its_rounds_on_the_lineage_and_closes_with_a_check(self):
        route = self.cell()
        node = route["nodes"][0]
        first = row(BLOCKING, "d1", "att-1")
        second = row(BLOCKING, "d2", "att-2")
        # the owner fixed (the digest moved): the next round is the closure check, not a fresh first review
        closure = self.decide(route, node, [first], "d2")
        self.assertEqual((closure.allowed, closure.state, closure.budget.round_kind, closure.budget.verdict_rounds),
                         (True, "admit", "closure-check", 1))
        # the owner changed nothing: no round is spent
        same = self.decide(route, node, [first], "d1")
        self.assertEqual((same.allowed, same.reason), (False, "review-subject-unchanged"))
        # the closure check also blocked and the owner fixed again: no third round, whatever the digest
        third = self.decide(route, node, [first, second], "d3")
        self.assertEqual((third.allowed, third.state, third.reason, third.budget.verdict_rounds),
                         (False, "exhausted", "review-round-budget-exhausted", 2))
        # a team route's review keeps the per-content budget: a new digest is a new budget (unchanged)
        team = self.preset("autopilot-code", "dev", "standard")
        team_node = next(n for n in team["nodes"] if n["id"] == "impl-review")
        again = self.decide(team, team_node, [first, second], "d3")
        self.assertTrue(again.allowed, again)
        # a round that produced no verdict is not counted; two in a row bind the node to the sealed fallback
        dead = ("done", {"attempt_id": "att-d", "note": "dead-capacity", "failure_class": "capacity",
                         "worker_type": "review", "review_subject_digest": "d1"})
        dead2 = ("done", {"attempt_id": "att-e", "note": "dead-capacity", "failure_class": "capacity",
                          "worker_type": "review", "review_subject_digest": "d1"})
        self.assertEqual(self.decide(route, node, [first, dead], "d2").state, "admit")
        bound = self.decide(route, node, [dead, dead2], "d1")
        self.assertEqual((bound.allowed, bound.state, bound.reason), (False, "verdictless-bound",
                                                                    "review-verdictless-bound"))

    def test_the_owner_cannot_close_the_review_of_its_own_work(self):
        route = self.cell()
        node = route["nodes"][0]
        spent = [row(BLOCKING, "d1", "att-1"), row(BLOCKING, "d2", "att-2")]
        for action in ("owner-closure", "complete-inline"):
            with self.subTest(action):
                refused = self.decide(route, node, spent, "d3", action=action)
                self.assertEqual((refused.allowed, refused.reason), (False, action + "-review-of-own-work"))
                # not even with no round left to run and nothing live
                self.assertFalse(self.decide(route, node, [], "d1", action=action).allowed)
        # a team conductor keeps both (unchanged)
        team = self.preset("autopilot-code", "dev", "standard")
        team_node = next(n for n in team["nodes"] if n["id"] == "impl-review")
        same_bytes = [row(BLOCKING, "d2", "att-1"), row(BLOCKING, "d2", "att-2")]
        self.assertTrue(self.decide(team, team_node, same_bytes, "d2", action="owner-closure").allowed)
        self.assertTrue(self.decide(team, team_node, [], "d1", action="complete-inline").allowed)



class CellTexts(Cell):
    """What the owner of the cell is told, and what is left unsaid for every other route."""

    def route_file(self, route, name="route.json"):
        path = Path(tempfile.mkdtemp(prefix="mc3b1-")) / name
        path.write_text(json.dumps(route), encoding="utf-8")
        self.addCleanup(lambda: path.unlink(missing_ok=True))
        return path

    def owner_args(self, route):
        return SimpleNamespace(worker_type="owner", route_file=str(self.route_file(route)))

    def test_the_task_gives_the_owner_the_one_command_that_starts_the_review(self):
        route = self.cell()
        args = self.owner_args(route)
        text = W.owner_route_steps_prompt(args, {})
        command = W.route_start_command(args.route_file, "impl-review")
        self.assertIn(f"`{command}`", text)
        for phrase in ("runtime_wait: registered-children", "fix every blocking finding in this worktree in one pass",
                       "start the closure check with the same command", "return your final handoff",
                       "cannot review your own work", "do not run `complete --inline`"):
            self.assertIn(phrase, text)
        # the command starts the review node of this route, and nothing else is printed as a start
        self.assertEqual(text.count("stage-dispatch-fallback.py"), 1)
        # the inline-completion advice a conductor gets is never given to this owner
        self.assertEqual(W.owner_inline_marker_prompt(args), "")
        self.assertNotIn("Start now", text)
        self.assertNotIn("conductor", text)

    def test_the_other_owners_hear_what_they_always_heard(self):
        team = self.preset("autopilot-code", "dev", "standard")
        args = self.owner_args(team)
        steps = W.owner_route_steps_prompt(args, {})
        self.assertIn("Route steps (sealed route", steps)
        self.assertNotIn("independent review (", steps)
        quick = self.compose(shape="solo", slug="texts-quick")
        quick_args = self.owner_args(quick)
        self.assertEqual(W.owner_route_steps_prompt(quick_args, {}), "")
        self.assertEqual(W.owner_inline_marker_prompt(quick_args), "")
        # a route with a depth-2 stage run by an owner that is no maker keeps the inline-completion advice
        refine = self.preset("autopilot-refine", "default", "standard")
        self.assertIn("complete --inline", W.owner_inline_marker_prompt(self.owner_args(refine)))

    def test_the_verdict_text_sends_the_owner_to_fix_or_to_end_failed(self):
        fields = {"kind": "review", "attempt": "att-r1", "artifact": "/c/report.md", "target": "", "source": True,
                  "command": "python3 start-review", "producer_command": "", "owner_fixes": True,
                  "closure_spent": False}
        first = W.continuation_followup_text(fields)
        self.assertIn("Fix every blocking finding in /c/report.md in this worktree in one pass", first)
        self.assertIn("then start the closure check with the same command: python3 start-review", first)
        self.assertNotIn("producing stage", first)
        spent = W.continuation_followup_text({**fields, "closure_spent": True})
        self.assertIn("Do not correct further", spent)
        self.assertIn("`verdict: FAIL`", spent)
        self.assertNotIn("python3 start-review", spent)
        # the team text is the bytes it was
        team = {"kind": "review", "attempt": "att-r1", "artifact": "/c/report.md", "target": "", "source": True,
                "command": "python3 start-review", "producer_command": "python3 start-execute"}
        self.assertEqual(W.continuation_followup_text(team),
                         "Review FAIL att-r1: batch the findings in /c/report.md into one correction of the source "
                         "through the bounded producing stage. First use python3 start-execute. "
                         "Then run the same start for the closure check: python3 start-review")

    def continuation(self, route):
        path = self.route_file(route)
        row = SimpleNamespace(status="open", metadata={"attempt_id": "att-owner"})
        gaps = {n["id"]: "completion-marker-absent" for n in route["nodes"] if n.get("dispatch_depth") == 2}
        import dispatch_completion_join as JOIN
        with mock.patch.object(JOIN, "exact_attempt_row", return_value=row), \
             mock.patch.object(DTC, "_completion_owner_choice", return_value=SimpleNamespace(allowed=True)), \
             mock.patch.object(DTC, "owner_workflow_gaps", return_value=gaps):
            return DTC.owner_workflow_continuation("/jobs.log", "att-owner", str(path)), path

    def test_an_owner_that_skipped_the_review_is_woken_with_the_start_command(self):
        text, path = self.continuation(self.cell())
        self.assertIn("[workflow-completion-pending]", text)
        command = W.route_start_command(path, "impl-review", jobs="/jobs.log")
        self.assertIn(f"`{command}`", text)
        self.assertIn("You cannot review your own work or close this review inline", text)
        # a team route's text is the bytes it was: no start command is added
        team_text, _ = self.continuation(self.preset("autopilot-code", "dev", "standard"))
        self.assertNotIn("stage-dispatch-fallback.py", team_text)
        self.assertTrue(team_text.endswith("Runtime settlement owns close/finalize."))
        self.assertNotIn("independent review has not run", team_text)

    def test_the_owner_with_a_review_waits_and_is_woken_so_no_other_turn_notice_is_given(self):
        cell = self.route_file(self.cell())
        quick = self.route_file(self.compose(shape="solo", slug="texts-q2"))
        self.assertFalse(OWNER_INPUT._owner_waits_on_review({"route_file": str(quick)}))
        self.assertTrue(OWNER_INPUT._owner_waits_on_review({"route_file": str(cell)}))
        value = {"attempt_id": "att-owner", "thread_id": "t", "transport": "claude-session", "accepting": True,
                 "requests": []}
        notes = {}
        for label, path in (("cell", cell), ("quick", quick)):
            with mock.patch.object(OWNER_INPUT, "_owner_phase", return_value="parked"):
                notes[label] = OWNER_INPUT._public(value, live=True, metadata={
                    "route_node": "one-shot", "intensity": "standard" if label == "cell" else "quick",
                    "route_file": str(path)})
        self.assertFalse(notes["cell"]["one_shot"])
        self.assertNotIn("one-shot owner", notes["cell"]["delivery_notice"])
        self.assertTrue(notes["quick"]["one_shot"])
        self.assertIn("This is a one-shot owner: another turn is not guaranteed", notes["quick"]["delivery_notice"])

    def test_d9_finalize_closes_only_a_route_one_process_finishes_alone(self):
        decision = lambda route: LIFECYCLE.finalize_direct_quick(  # noqa: E731
            Path(self.root), LIFECYCLE.DirectQuickRequest(route=route, route_file=Path("/nonexistent/route.json")))
        rejected = {"d9-intensity-not-direct-or-quick"}
        for label, route in (("cell", self.cell()), ("team", self.preset("autopilot-code", "dev", "standard")),
                             ("junk", ["not", "a", "route"])):
            with self.subTest(label):
                got = decision(route)
                self.assertEqual((got.status, {r.code for r in got.reasons}), ("reject", rejected))
        for label, route in (("solo quick", self.compose(shape="solo", slug="d9-quick")),
                             ("direct", self.compose(shape="direct", slug="d9-direct"))):
            with self.subTest(label):
                self.assertNotEqual({r.code for r in decision(route).reasons}, rejected)
        # the cell has no marker-free close: the route-only close would write an unproven terminal gate
        self.assertFalse(RF.alone_without_stages(self.cell()))

    def test_a_pin_change_for_the_cell_is_probed_for_the_owner_and_for_the_review(self):
        route = self.cell()
        calls = []

        def readiness(cwd, jobs, parent, children, **kw):
            calls.append((parent, tuple(children)))
            return {"tuples": [{"parent_harness": parent, "child_harness": child, "status": "supported",
                                "launch_authority": "conductor"} for child in children],
                    "candidates": [{"harness": parent, "status": "supported"}]}
        with mock.patch.object(COMPOSE, "_compose_readiness", readiness), \
             mock.patch.object(COMPOSE.DEFAULTS, "current_enabled_harnesses", return_value={"claude", "codex"}), \
             mock.patch("route_authority.route_in_force", side_effect=lambda r: r):
            tuples, candidates = COMPOSE._pin_change_probe(route, "/jobs.log", "owner", "codex", "claude")
            self.assertTrue(tuples and candidates)           # both: the owner's candidates, the review's tuples
            tuples, candidates = COMPOSE._pin_change_probe(route, "/jobs.log", "worker", "claude", "claude")
            self.assertTrue(tuples and not candidates)       # the review's tuples alone
            quick = self.compose(shape="solo", slug="pin-quick")
            self.assertEqual(COMPOSE._pin_change_probe(quick, "/jobs.log", "worker", "claude", "claude"), ([], []))
            tuples, candidates = COMPOSE._pin_change_probe(quick, "/jobs.log", "owner", "codex", "claude")
            self.assertEqual((tuples, bool(candidates)), ([], True))
        self.assertEqual(len(calls), 3)


if __name__ == "__main__":
    unittest.main()
