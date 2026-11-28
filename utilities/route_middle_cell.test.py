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
import shlex
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
import capability_route_close as CLOSE  # noqa: E402
import work_start as WS  # noqa: E402


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
        # the review's chain is the declared registered headless hops only: no `inline` and no native subagent
        # (its completion is recorded inline), since the maker cannot close its own review
        self.assertEqual([hop["fallback_hop"] for hop in review["fallback_hops"]],
                         ["same-harness-headless", "cross-harness-headless"])
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
                          quick["owner_model_profile"]), (["one-shot"], "quick", 1, "balanced"))
        with self.assertRaisesRegex(ValueError, r"compose-shape-intensity-mismatch:solo \(allowed: quick,standard,strong,"
                                                r"thorough,adversarial\)"):
            self.compose(shape="solo", intensity="direct", slug="around-direct")
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
        self.assertEqual(self.preset("autopilot-code", "dev", "quick")["owner_model_profile"], "balanced")

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
            "fallback_hops": ["same-harness-headless", "cross-harness-headless"], "verify_node": "test"})
        # no native or inline hop in the chain: a registered reviewer is the only one that can close this review
        for hop in ("inline", "native-subagent"):
            bad = json.loads(json.dumps(recipe))
            bad["one_shot_review"]["fallback_hops"].append(hop)
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


    # -- the command that composes an autoclosed route's work again ----------------------------------------
    def compose_again_argv(self, route):
        # the task file the command names is written under the route's artifact root: keep it in the fixture's
        route = {**route, "artifact_root": str(self.root)}
        argv = shlex.split(WS._compose_again(route))
        flag = lambda name: argv[argv.index(name) + 1] if name in argv else None  # noqa: E731
        return argv, flag("--shape"), flag("--intensity"), flag("--graph")

    def test_composing_an_autoclosed_route_again_builds_its_own_form_at_its_own_intensity(self):
        """The command is read from the closed route's nodes and intensity, not from the word recorded for its
        shape: the cell does not come back as a `solo·quick` with no review, and the team route an older
        release sealed under the word `solo` comes back as a team route."""
        cell = self.cell()
        quick = self.compose(shape="solo", slug="again-quick")
        old_team = self.solo_signal_team_route()
        staged = self.compose(shape="staged", graph="execute,test,report", intensity="strong", slug="again-staged")
        unit = self.unit_route(slug="again-unit")[0]
        team = ["plan", "plan-check", "execute", "impl-review", "test", "report"]
        rows = [  # label, route, --shape, --intensity, the nodes composing that argv gives
            ("the cell", cell, "solo", "standard", ["impl-review", "one-shot"]),
            ("solo quick", quick, "solo", None, ["one-shot"]),
            ("older solo + signal (team nodes)", old_team, "staged", "standard", team),
            ("staged", staged, "staged", "strong", ["execute", "test", "report"]),
        ]
        for label, route, shape, intensity, nodes in rows:
            with self.subTest(label):
                argv, got_shape, got_intensity, graph = self.compose_again_argv(route)
                self.assertEqual((got_shape, got_intensity), (shape, intensity))
                again = self.compose(shape=got_shape, intensity=got_intensity, graph=graph, slug="again-" + shape)
                self.assertEqual([n["id"] for n in again["nodes"]], nodes)
                self.assertEqual(again["effective_intensity"], route["effective_intensity"])
                self.assertEqual(RF.one_shot_review_route(again), RF.one_shot_review_route(route))
                self.verify(again)
        quick_argv = self.compose_again_argv(quick)[0]
        self.assertNotIn("--intensity", quick_argv)
        # a unit route is still named by its unit, never read as a team route
        unit_argv = self.compose_again_argv(unit)[0]
        self.assertEqual((unit_argv[unit_argv.index("--unit") + 1], "--intensity" in unit_argv, "--capability" in unit_argv),
                         (unit["nodes"][0]["unit"], False, False))

    # -- the owner's marker cannot stand in for the review (the end gate) -------------------------------
    def end_gate_row(self, route, review):
        """The terminal gate row of `route` with an owner marker on disk and the review's marker in the state
        `review` names (`None`: the marker passes; otherwise the reason it does not)."""
        jobs = Path(self.root) / "jobs.log"
        jobs.write_text("", encoding="utf-8")
        owner = route["nodes"][-1]
        marker = CLOSE.completion_dir(route["route_id"], jobs=jobs) / (owner["id"] + ".json")
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("{}", encoding="utf-8")

        def identity(_route, node, node_id, gate, **kw):
            if node_id == owner["id"] or review is None:
                return {"passed": True, "reason": "completion-marker-verified"}
            return {"passed": False, "reason": review}
        with mock.patch.object(CLOSE, "_marker_identity_row", side_effect=identity):
            return CLOSE.terminal_gate_observation(route, jobs=jobs)[owner["id"]]

    def test_an_owner_marker_does_not_stand_in_for_the_cells_review(self):
        cell, quick = self.cell(), self.compose(shape="solo", slug="gate-quick")
        # the owner's marker is there and the review has no marker, or its marker is stale: the end is not proven
        for review in ("completion-marker-absent", "completion-review-source-stale"):
            with self.subTest(review):
                row = self.end_gate_row(cell, review)
                self.assertFalse(row["passed"], row)
                self.assertTrue(row["reason"].startswith("owner-prerequisite-unproven:"), row)
                self.assertIn(review, row["reason"])
        # the review passed (current): the owner's marker then proves the end
        self.assertTrue(self.end_gate_row(cell, None)["passed"])
        # `solo·quick` has no review to wait on: the owner's marker alone passes, as it always did
        self.assertTrue(self.end_gate_row(quick, "completion-marker-absent")["passed"])


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

    def continuation(self, route, reason="completion-marker-absent", rows=(), fields=None):
        path = self.route_file(route)
        row = SimpleNamespace(status="open", metadata={"attempt_id": "att-owner"})
        gaps = {n["id"]: reason for n in route["nodes"] if n.get("dispatch_depth") == 2}
        import dispatch_completion_join as JOIN
        with mock.patch.object(JOIN, "exact_attempt_row", return_value=row), \
             mock.patch.object(DTC, "_completion_owner_choice", return_value=SimpleNamespace(allowed=True)), \
             mock.patch.object(DTC, "owner_workflow_gaps", return_value=gaps), \
             mock.patch.object(W, "_lineage_rows", return_value={"impl-review": list(rows)}), \
             mock.patch.object(W, "continuation_followup_fields", return_value=fields or {}):
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

    def test_the_wake_text_says_what_is_true_of_the_review_for_each_reason(self):
        """The sentence after the pending notice follows why the review does not stand: it has not run, it ran and
        passed work the owner changed since, it blocked (the verdict text's own instruction) or its rounds are spent."""
        route = self.cell()
        passed = ("done", {"attempt_id": "att-p", "note": "completed-marker", "failure_class": "pass",
                           "worker_type": "review", "review_subject_digest": "d1"})
        dead = ("done", {"attempt_id": "att-d", "note": "dead-capacity", "failure_class": "capacity",
                         "worker_type": "review", "review_subject_digest": "d1"})
        blocked = row(BLOCKING, "d1", "att-1")
        fields = {"kind": "review", "attempt": "att-1", "artifact": "/c/report.md", "target": "", "source": True,
                  "command": "python3 start-review", "producer_command": "", "owner_fixes": True,
                  "closure_spent": False}
        tail = " You cannot review your own work or close this review inline; do not report PASS without it."
        unrun, path = self.continuation(route)
        # the sentence a review that has not run has always got, byte for byte
        self.assertTrue(unrun.endswith(
            "\nYour route's independent review has not run. Commit your work, then start it with exactly: "
            f"`{W.route_start_command(path, 'impl-review', jobs='/jobs.log')}` and yield "
            "runtime_wait: registered-children." + tail))
        stale, path = self.continuation(route, reason="completion-review-source-stale", rows=[passed])
        self.assertIn("\nYour route's independent review passed, but the work changed after it. Commit your work, "
                      "then start the review again with exactly: ", stale)
        self.assertNotIn("has not run", stale)
        self.assertIn("stage-dispatch-fallback.py", stale)
        again, path = self.continuation(route, rows=[dead])
        self.assertIn("independent review ended without a verdict", again)
        self.assertNotIn("has not run", again)
        # a blocking verdict: the verdict text's own instruction, not "has not run"
        blocks, path = self.continuation(route, rows=[blocked], fields=fields)
        self.assertTrue(blocks.endswith("\n" + W.continuation_followup_text(fields)))
        self.assertIn("Fix every blocking finding in /c/report.md", blocks)
        self.assertNotIn("has not run", blocks)
        # the rounds spent: no start is asked for; the owner ends failed, as the verdict text says
        spent, path = self.continuation(route, rows=[blocked, blocked], fields={**fields, "closure_spent": True})
        self.assertTrue(spent.endswith("\n" + W.continuation_followup_text({**fields, "closure_spent": True})))
        self.assertIn("Do not correct further", spent)
        self.assertIn("`verdict: FAIL`", spent)
        self.assertNotIn("stage-dispatch-fallback.py", spent)
        self.assertNotIn("has not run", spent)
        # a blocking verdict whose report cannot be projected still says the review blocks (never "has not run")
        unread, path = self.continuation(route, rows=[blocked], fields={})
        self.assertIn("independent review blocks your current work", unread)
        self.assertNotIn("has not run", unread)

    def test_the_task_states_the_review_limit_and_the_next_ready_line_does_not_contradict_a_block(self):
        text = W.owner_route_steps_prompt(self.owner_args(self.cell()), {})
        self.assertIn("The review gets 2 rounds in all: if the closure check blocks too, correct no further, write "
                      "the remaining findings in your report, and end with `verdict: FAIL`.", text)
        import dispatch_completion_join as JOIN
        import dispatch_stage_advance as ADVANCE
        for label, route, node_id, successor in (("cell", self.cell(), "impl-review", "one-shot"),
                                                 ("team", self.preset("autopilot-code", "dev", "standard"),
                                                  "impl-review", "test")):
            path = self.route_file(route)
            successor = next(n for n in route["nodes"] if n["id"] == successor)
            lines = {}
            for note in ("completed-review-blocking", "completed-marker"):
                meta = {"dispatch_depth": "2", "worker_type": "review", "route_file": str(path),
                        "route_id": route["route_id"], "route_hash": route["route_hash"], "note": note,
                        "route_node": node_id, "attempt_id": "att-r"}
                with mock.patch.object(JOIN, "exact_attempt_row", return_value=SimpleNamespace(metadata=meta)), \
                     mock.patch.object(ADVANCE, "completed_nodes", return_value=set()), \
                     mock.patch.object(ADVANCE, "started_nodes", return_value=set()), \
                     mock.patch.object(ADVANCE, "runnable_successors", return_value=[successor]):
                    lines[note] = JOIN._next_ready_start_lines({"children": [{"attempt_id": "att-r"}]}, "/jobs.log")
            with self.subTest(label):
                blocking, passing = lines["completed-review-blocking"], lines["completed-marker"]
                if label == "cell":
                    self.assertEqual(passing, ["Next ready: nothing to start; the independent review has finished. "
                                               "Act on its verdict as your task describes, then return your final handoff."])
                    self.assertEqual(len(blocking), 1)
                    self.assertIn("Follow the Review FAIL instruction above", blocking[0])
                    self.assertNotIn("then return your final handoff", blocking[0])
                else:   # a team conductor's line is the start command, blocking or not
                    self.assertEqual(blocking, passing)
                    self.assertTrue(blocking[0].startswith("Next ready: python3 "))

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
