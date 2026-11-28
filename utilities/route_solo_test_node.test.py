#!/usr/bin/env python3
"""`solo` from `strong` (route form S3): after its review legs, the route borrows its kind's test node
(`one_shot_review.verify_node`), and the owner's end waits on that test too.

Real compose and verify. The test node is the recipe's own `test` (unit `qa/test`, its model, outputs and gate),
with the cell's inputs and hops; it waits on every review leg. The owner cannot close it, its rounds count on its
own lineage, the `Next ready:` line names its start once every review stands, and a failing test sends the fixed work
through the reviews again (the one command the owner's task gives).
"""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import importlib.util
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))


def _load(name, filename):  # noqa: E302
    spec = importlib.util.spec_from_file_location(name, HERE / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


S2 = _load("route_solo_intensity_for_test_node", "route_solo_intensity.test.py")
CELL, R, RF, AUTH, W, DTC, JOIN = S2.CELL, S2.R, S2.RF, S2.AUTH, S2.W, S2.DTC, S2.JOIN
HOPS, CAP = S2.HOPS, S2.CAP


def failed_test(digest, attempt):
    """A finished test that failed: the test runs as a stage worker, so its FAIL is `dead-worker-fail`/`fail`."""
    return ("done", {"attempt_id": attempt, "note": "dead-worker-fail", "failure_class": "fail", "worker_type": "stage",
                     "review_subject_digest": digest})


class SoloTestNode(S2.Base):
    route_file = S2.SoloIntensityTexts.route_file
    owner_args = S2.SoloIntensityTexts.owner_args

    def test_from_strong_the_compiler_the_preview_and_verify_build_the_test_after_every_review_leg(self):
        for intensity, legs in S2.LEGS.items():
            with self.subTest(intensity):
                route = self.solo(intensity)
                self.verify(route)
                reviews = [leg[0] for leg in legs]
                test = next(n for n in route["nodes"] if n["id"] == "test")
                recipe_test = next(n for n in R.TOPO.resolve_recipe(R.TOPO.load_registry(), "autopilot-code", "dev")
                                   ["standard_plus"]["nodes"] if n["id"] == "test")
                self.assertEqual(self.ids(route), [*reviews, "test", "one-shot"])
                self.assertEqual((test["unit"], test["model_profile"], test["completion_gate"], test["outputs"],
                                  test["write_scope"], test["dispatch_depth"]),
                                 (recipe_test["unit"], "light", "code-test", recipe_test["outputs"],
                                  recipe_test["write_scope"], 2))
                self.assertEqual((test["depends_on"], test["inputs"], [h["fallback_hop"] for h in test["fallback_hops"]]),
                                 (reviews, ["source-diff"], HOPS))
                self.assertEqual(sorted(route["nodes"][-1]["depends_on"]), sorted([*reviews, "test"]))
                self.assertIn("code-test", route["completion_gates"])
                self.assertEqual(RF.one_shot_test_node(route)["id"], "test")
                preview = R.compose_route(
                    capability="autopilot-code", capability_mode="dev", shape="solo", graph=None, slug="preview",
                    cwd=R.ROOT, artifact_root=self.root, spec_read="fixture", campaign_key="form-key",
                    intensity=intensity, selection_only=True)
                self.assertEqual([n["id"] for n in preview["nodes"]], self.ids(route))

    def main_registry(self):
        """The registry before this slice: today's, without the one key it adds (`one_shot_review.verify_node`)."""
        registry = json.loads(json.dumps(R.TOPO.load_registry()))
        for recipe in registry["recipes"]:
            (recipe.get("one_shot_review") or {}).pop("verify_node", None)
        return registry

    def test_routes_sealed_before_the_test_keep_their_graph_progress_and_text(self):
        # S3 adds one registry key, so a route sealed before it has another registry digest and route id (as after
        # every registry change; a team owner's text carries its route id). What stays: an open route keeps its
        # sealed graph through verify, start and close and only reads `_registry_current=False`, and a new compile of
        # the same input builds the graph and owner text it built before, for every cell but `solo` from `strong`.
        import capability_route_close as CLOSE
        import dispatch_contract as DC
        from route_lineage import canonical_route_path
        main, real = self.main_registry(), R.TOPO.load_registry
        cells = {"solo quick": dict(shape="solo", intensity="quick"),
                 "solo standard": dict(shape="solo", intensity="standard"),
                 "team": dict(shape="staged", intensity="strong"),
                 "solo strong (S2)": dict(shape="solo", intensity="strong")}
        graph = lambda route: [(n["id"], n.get("unit"), n.get("model_profile"), n.get("depends_on"),  # noqa: E731
                                n.get("completion_gate")) for n in route["nodes"]]

        def owner_text(route):
            args = self.owner_args(route)
            return (W.owner_route_steps_prompt(args, {}).replace(args.route_file, "<route-file>")
                    .replace(route["route_id"], "<route-id>").replace(route["route_hash"], "<route-hash>"))
        jobs = Path(self.root) / "dispatch" / "jobs.log"
        jobs.parent.mkdir(parents=True, exist_ok=True)
        jobs.write_text("", encoding="utf-8")
        for index, (label, kw) in enumerate(cells.items()):
            with self.subTest(label):
                slug = f"s3-sealed-{index}"
                with mock.patch.object(R.TOPO, "load_registry",
                                       side_effect=lambda *a, **k: real(*a, **k) if a or k else json.loads(json.dumps(main))):
                    before = self.compose(slug=slug, **kw)
                    self.assertIs(R.verify_route(json.loads(json.dumps(before)), before["cwd"])["_registry_current"],
                                  True)
                now = self.compose(slug=slug, **kw)
                self.assertNotEqual((before["registry_digest"], before["route_id"]), (now["registry_digest"], now["route_id"]))
                if label != "solo strong (S2)":
                    self.assertEqual(graph(now), graph(before))
                    self.assertEqual(owner_text(now), owner_text(before))
                # the open route under the new registry: verify (the start path's own check) keeps the sealed graph
                verified = R.verify_route(json.loads(json.dumps(before)), before["cwd"])
                self.assertIs(verified["_registry_current"], False)
                self.assertEqual(graph(verified), graph(before))
                self.assertIsNone(RF.one_shot_test_node(verified))
                route_file = canonical_route_path(before["artifact_root"], before["route_id"])
                route_file.parent.mkdir(parents=True, exist_ok=True)
                route_file.write_text(json.dumps(before), encoding="utf-8")
                # start: the first node's shared start gate admits it as the sealed graph declares
                first = next(n["id"] for n in verified["nodes"] if not n.get("depends_on"))
                DC.completion_marker_gate(str(route_file), first, "dry-run", Path(self.root), jobs)
                if RF.one_shot_review_route(verified) and label != "solo quick":
                    # the owner's end waits on the sealed review legs only, and its task names no test
                    owner = next(n for n in verified["nodes"] if n["id"] == "one-shot")
                    self.assertEqual(sorted(CLOSE.owner_terminal_prerequisites(verified, owner, jobs)),
                                     sorted(n["id"] for n in verified["nodes"] if n.get("dispatch_depth") == 2))
                    self.assertNotIn("independent test", W.one_shot_review_prompt(verified, str(route_file)))
                # close: the registry verdict is a record; the gates are the sealed graph's
                outcome, _ = CLOSE.close_route(verified, route_file, commit="a" * 40, summary="fixture",
                                               registry_current=verified["_registry_current"])
                self.assertIs(outcome["registry_current"], False)
                if label != "team":                                     # a team route seals its own test
                    self.assertNotIn('"test"', json.dumps(outcome["terminal_gates"]))

    def test_standard_has_no_test_and_its_record_is_unchanged(self):
        route = self.solo("standard")
        self.assertEqual(self.ids(route), ["impl-review", "one-shot"])
        self.assertIsNone(RF.one_shot_test_node(route))
        self.assertEqual(route["completion_gates"], ["code-impl-review", "quick-complete"])

    def test_a_kind_that_declares_no_verify_node_keeps_the_review_legs_alone(self):
        recipe = json.loads(json.dumps(R.TOPO.resolve_recipe(R.TOPO.load_registry(), "autopilot-code", "dev")))
        self.assertEqual(recipe["one_shot_review"]["verify_node"], "test")
        recipe["one_shot_review"].pop("verify_node")
        self.assertEqual([n["id"] for n in R.one_shot_review_nodes(recipe, "strong")],
                         ["impl-review", "impl-review-alternative"])
        bad = json.loads(json.dumps(recipe))
        bad["one_shot_review"]["verify_node"] = "execute"
        registry = R.TOPO.load_registry()
        with self.assertRaisesRegex(R.TOPO.TopologyError, "verify_node"):
            R.TOPO._validate_recipe(bad, registry, registry["owner_profile_by_intensity"]["standard"])

    def test_verify_refuses_a_cell_whose_test_differs_from_the_declaration(self):
        strong = self.solo("strong")
        copy = lambda route: json.loads(json.dumps(route))  # noqa: E731
        edits = {}
        no_test = copy(strong)
        no_test["nodes"] = [n for n in no_test["nodes"] if n["id"] != "test"]
        no_test["nodes"][-1]["depends_on"] = ["impl-review", "impl-review-alternative"]
        edits["strong without its test"] = no_test
        one_leg = copy(strong)
        one_leg["nodes"][2]["depends_on"] = ["impl-review"]
        edits["a test that waits on one leg only"] = one_leg
        end = copy(strong)
        end["nodes"][3]["depends_on"] = ["impl-review", "impl-review-alternative"]
        edits["an end that does not wait on the test"] = end
        inline = copy(strong)
        inline["nodes"][2]["fallback_hops"].append({"ordinal": 4, "fallback_hop": "inline",
                                                    "status": "eligible-after-prior-hop-exhaustion",
                                                    "reason_enum": "runtime-unavailable", "fleet_visibility": "none"})
        edits["an inline hop on the test"] = inline
        planned = copy(strong)
        planned["nodes"][2]["inputs"] = ["plan.md", "checklist.md", "source-diff"]
        edits["a plan as the test's input"] = planned
        for label, edited in edits.items():
            with self.subTest(label):
                with self.assertRaises(ValueError):
                    self.verify(CELL.reseal(edited))

    def test_the_owner_cannot_close_the_test_and_its_rounds_count_on_its_lineage(self):
        for intensity in ("strong", "thorough"):
            route = self.solo(intensity)
            test = RF.one_shot_test_node(route)
            cap = CAP[intensity]
            rows = [failed_test(f"d{i}", f"att-{i}") for i in range(1, cap + 1)]
            with self.subTest(intensity=intensity):
                for action in ("owner-closure", "complete-inline"):
                    refused = self.decide(route, test, rows, f"d{cap + 1}", action=action)
                    self.assertEqual((refused.allowed, refused.reason), (False, action + "-review-of-own-work"))
                # the owner fixed after a failing test: another round on the test's own lineage
                again = self.decide(route, test, rows[:1], "d2")
                self.assertEqual((again.allowed, again.budget.verdict_rounds), (True, 1))
                # every round of the cap used, whatever the bytes: no more
                spent = self.decide(route, test, rows, f"d{cap + 1}")
                self.assertEqual((spent.allowed, spent.state, spent.budget.verdict_rounds), (False, "exhausted", cap))
                # the fallback check reads the same verification nodes: the test's chain is the cell's two hops
                self.assertIn(test["id"], RF.one_shot_review_node_ids(route))
                self.assertEqual([h["fallback_hop"] for h in test["fallback_hops"]], HOPS)

    def decide(self, route, node, rows, digest, action="round"):
        return AUTH.decide_continuation(route, action=action, node=node, rows=rows,
                                        facts={"source_subject_digest": digest})

    def test_a_leg_that_passed_before_the_owner_changed_the_work_starts_a_new_round_not_a_replay(self):
        # Every leg passed, the test failed and the owner fixed: the group's next start judges the change. A leg
        # whose pass describes the current source, a team route's leg, and a blocking leg keep what they decided.
        node_module = _load("dispatch_node_for_test_node", "dispatch-node.py")
        solo, team = self.solo("strong"), self.compose(shape="staged", intensity="strong",
                                                       graph="execute,impl-review,test,report", slug="s3-team")

        def admission(route, node_id, note, judged, current):
            node = next(n for n in route["nodes"] if n["id"] == node_id)
            meta = {"attempt_id": "att-1", "note": note, "failure_class": "pass" if note == CELL.PASSING else "review",
                    "worker_type": "review", "review_subject_digest": judged}
            with mock.patch.object(node_module, "prior_round_attempts", return_value=[(["t", "done"], meta)]), \
                 mock.patch("review_source_subject.observe", return_value={"digest": current, "manifest": []}), \
                 mock.patch("review_source_subject.read_binding", return_value=None), \
                 mock.patch.object(node_module.ROUTE, "_dependency_revisions", return_value=[]):
                return node_module.admit_round(route, node, "/jobs.log", record_auto_revisions=False)
        self.assertTrue(admission(solo, "impl-review", CELL.PASSING, "d1", "d2").requires_next_round)
        self.assertFalse(admission(solo, "impl-review", CELL.PASSING, "d2", "d2").requires_next_round)
        self.assertFalse(admission(team, "impl-review", CELL.PASSING, "d1", "d2").requires_next_round)
        self.assertTrue(admission(solo, "impl-review-alternative", CELL.BLOCKING, "d1", "d2").requires_next_round)

    # -- what the owner is told -----------------------------------------------------------------------------------
    def test_the_task_names_the_review_legs_and_the_test_and_gives_one_command(self):
        for intensity, legs in S2.LEGS.items():
            with self.subTest(intensity):
                args = self.owner_args(self.solo(intensity))
                text = W.owner_route_steps_prompt(args, {})
                self.assertIn(f"`{W.route_start_command(args.route_file, 'impl-review')}`", text)
                self.assertEqual(text.count("stage-dispatch-fallback.py"), 1)
                self.assertIn(f"{len(legs)} independent reviews of it in one review group", text)
                self.assertIn("and then one independent test of it (test, qa/test) once every review passes", text)
                self.assertIn("the `Next ready:` line gives the test's start command", text)
                self.assertIn("If the test fails, fix what it found in one pass", text)
                self.assertIn("start the review group again with the same command", text)

    def test_a_failing_test_is_corrected_no_further_once_the_test_or_a_review_has_used_its_rounds(self):
        route = self.solo("strong")
        passed = lambda attempt: ("done", {"attempt_id": attempt, "note": CELL.PASSING, "failure_class": "pass",  # noqa: E731
                                           "worker_type": "review"})
        blocked = CELL.row(CELL.BLOCKING, "d1", "att-b")
        cases = (({"impl-review": [passed("a1")], "impl-review-alternative": [passed("b1")],
                   "test": [failed_test("d1", "t1")]}, False),
                 ({"impl-review": [passed("a1")], "impl-review-alternative": [passed("b1")],
                   "test": [failed_test("d1", "t1"), failed_test("d2", "t2")]}, True),
                 # a leg blocked once and passed the correction: its two rounds are used, it cannot judge another fix
                 ({"impl-review": [passed("a1"), passed("a2")], "impl-review-alternative": [blocked, passed("b2")],
                   "test": [failed_test("d2", "t1")]}, True))
        for lineage, spent in cases:
            with mock.patch.object(W, "_lineage_rows", return_value=lineage):
                self.assertEqual(W.one_shot_test_spent(route, "/jobs.log"), spent, lineage)

    def next_ready(self, route, notes, missing, node_id="impl-review"):
        import dispatch_stage_advance as ADVANCE
        path = self.route_file(route)
        metas = {node: {"dispatch_depth": "2", "worker_type": "review", "route_file": str(path),
                        "route_id": route["route_id"], "route_hash": route["route_hash"], "note": note,
                        "route_node": node, "attempt_id": "att-" + node} for node, note in notes.items()}
        receipt = {"children": [{"attempt_id": "att-" + node} for node in notes]}
        started = {n["id"] for n in route["nodes"]}       # every node has run once: only current markers decide
        with mock.patch.object(JOIN, "exact_attempt_row",
                               side_effect=lambda _jobs, attempt: SimpleNamespace(metadata=metas[attempt[4:]])), \
             mock.patch.object(ADVANCE, "completed_nodes", return_value=set(started)), \
             mock.patch.object(ADVANCE, "started_nodes", return_value=started), \
             mock.patch.object(ADVANCE.ROUTE, "owner_terminal_prerequisites", return_value=missing):
            return JOIN._next_ready_start_lines(receipt, "/jobs.log"), path

    def test_the_next_ready_line_names_the_test_once_every_review_stands(self):
        route = self.solo("strong")
        passed = {"impl-review": CELL.PASSING, "impl-review-alternative": CELL.PASSING}
        lines, path = self.next_ready(route, passed, {"test": "completion-marker-absent"})
        self.assertEqual(lines, ["Next ready: " + W.route_start_command(path, "test")])
        # a leg blocks: the verdict text is the instruction, never the test
        lines, _ = self.next_ready(route, {**passed, "impl-review-alternative": CELL.BLOCKING},
                                   {"test": "completion-marker-absent"})
        self.assertFalse(any("--node test" in line for line in lines), lines)
        # a review that is not current (a pass kept from before the last fix) is still missing: no test yet
        lines, _ = self.next_ready(route, passed, {"test": "completion-marker-absent",
                                                   "impl-review": "completion-review-source-stale"})
        self.assertFalse(any("--node test" in line for line in lines), lines)
        # the test itself finished, or stands already: nothing to start
        lines, _ = self.next_ready(route, {"test": CELL.PASSING}, {}, node_id="test")
        self.assertFalse(any("--node test" in line for line in lines), lines)
        lines, _ = self.next_ready(route, passed, {})
        self.assertFalse(any("--node test" in line for line in lines), lines)

    def test_a_failing_test_sends_the_fixed_work_through_the_reviews_again(self):
        route = self.solo("strong")
        failed = failed_test("d1", "att-t")
        passed = ("done", {"attempt_id": "att-p", "note": CELL.PASSING, "failure_class": "pass", "worker_type": "review",
                           "review_subject_digest": "d1"})
        fields = {"kind": "review", "attempt": "att-t", "artifact": "/c/test.md", "target": "", "source": True,
                  "producer_command": "", "owner_fixes": True, "test": True, "command": "python3 start-group",
                  "closure_spent": False, "corrections": 1}
        lineage = {"impl-review": [passed], "impl-review-alternative": [passed], "test": [failed]}
        # only the test is missing and it fails: the test's own instruction (reviews first again)
        text, path = self.wake(route, lineage=lineage, fields={"test": fields},
                               reasons={"impl-review": None, "impl-review-alternative": None})
        self.assertTrue(text.endswith("\n" + W.continuation_followup_text(fields)), text)
        follow = W.continuation_followup_text(fields)
        self.assertIn("Test FAIL att-t: the independent test of your work fails.", follow)
        self.assertIn("start the reviews again with the same command: python3 start-group", follow)
        spent = W.continuation_followup_text({**fields, "closure_spent": True})
        self.assertIn("Do not correct further", spent)
        self.assertIn("`verdict: FAIL`", spent)
        self.assertNotIn("python3 start-group", spent)
        # its report cannot be projected: the wake text still sends the work through the review group
        text, path = self.wake(route, lineage=lineage, reasons={"impl-review": None, "impl-review-alternative": None})
        self.assertIn("independent test fails on your current work", text)
        self.assertIn(f"`{W.route_start_command(path, 'impl-review', jobs='/jobs.log')}`", text)
        # the owner fixed after the failing test and the reviews passed on the change: the test runs again
        text, path = self.wake(route, lineage=lineage, fields={"test": fields}, current="d2",
                               reasons={"impl-review": None, "impl-review-alternative": None})
        self.assertIn("independent test failed on earlier work, and the reviews passed on your change", text)
        self.assertIn(f"`{W.route_start_command(path, 'test', jobs='/jobs.log')}`", text)
        self.assertNotIn("Test FAIL", text)
        # only the test has not run: its own start command
        text, path = self.wake(route, lineage={"impl-review": [passed], "impl-review-alternative": [passed]},
                               reasons={"impl-review": None, "impl-review-alternative": None})
        self.assertIn("Your route's independent test has not run.", text)
        self.assertIn(f"`{W.route_start_command(path, 'test', jobs='/jobs.log')}`", text)
        # the reviews are missing too (the fix made them stale): the review group first, the test's old FAIL unnamed
        text, path = self.wake(route, reason="completion-review-source-stale", lineage=lineage, fields={"test": fields},
                               reasons={"test": "completion-marker-absent"})
        self.assertIn("independent review passed, but the work changed after it", text)
        self.assertIn(f"`{W.route_start_command(path, 'impl-review', jobs='/jobs.log')}`", text)
        self.assertNotIn("Test FAIL", text)

    def test_a_failing_test_that_can_be_judged_no_more_ends_the_owner_whether_or_not_its_report_reads(self):
        # The rounds a further judgment needs decide, not whether the test's report can be read: the test alone once
        # the reviews passed a correction, otherwise a correction's reviews and then the test.
        route = self.solo("strong")
        passed = lambda attempt, digest: ("done", {"attempt_id": attempt, "note": CELL.PASSING,  # noqa: E731
                                                   "failure_class": "pass", "worker_type": "review",
                                                   "review_subject_digest": digest})
        dead = ("done", {"attempt_id": "t-dead", "note": "dead-runtime-exit", "failure_class": "runtime",
                         "worker_type": "stage", "review_subject_digest": "d2"})
        legs = ("impl-review", "impl-review-alternative")
        stand = {leg: None for leg in legs}
        report = {"kind": "review", "attempt": "t2", "artifact": "/c/test.md", "target": "", "source": True,
                  "producer_command": "", "owner_fixes": True, "test": True, "command": "python3 start-group",
                  "closure_spent": True, "corrections": 1}
        twice = {**{leg: [passed(leg + "-1", "d1"), passed(leg + "-2", "d2")] for leg in legs},
                 "test": [failed_test("d1", "t1"), failed_test("d2", "t2")]}
        for label, fields in (("a FAIL that names its report", {"test": report}),
                              ("a FAIL whose report is absent or gone since", {})):
            with self.subTest(label):
                text, path = self.wake(route, lineage=twice, fields=fields, current="d2", reasons=stand)
                self.assertIn("Test FAIL t2: the test still fails, and the one correction allowed is spent. Do not "
                              "correct further.", text)
                self.assertIn("end with `verdict: FAIL`.", text)
                self.assertEqual("from /c/test.md in your report" in text, bool(fields), text)
                self.assertNotIn("start the reviews again", text)
                self.assertNotIn("stage-dispatch-fallback", text)
        # the test failed once on the work the reviews passed, and a leg has used its rounds: no correction can be
        # reviewed, so the same end, with no report to read
        spent_leg = {"impl-review": [passed("a1", "d0"), passed("a2", "d1")],
                     "impl-review-alternative": [CELL.row(CELL.BLOCKING, "d0", "b1"), passed("b2", "d1")],
                     "test": [failed_test("d1", "t1")]}
        text, _ = self.wake(route, lineage=spent_leg, current="d1", reasons=stand)
        self.assertIn("Test FAIL t1: the test still fails", text)
        self.assertIn("Do not correct further", text)
        # the boundary: the owner fixed after the test's FAIL and the reviews passed the change on their last
        # rounds; the test has a round left, so it starts again
        reviewed = {**{leg: [passed(leg + "-1", "d1"), passed(leg + "-2", "d2")] for leg in legs},
                    "test": [failed_test("d1", "t1")]}
        for label, rows in (("after the FAIL", reviewed), ("after a death without a verdict", {
                **reviewed, "test": [failed_test("d1", "t1"), dead]})):
            with self.subTest(label):
                text, path = self.wake(route, lineage=rows, current="d2", reasons=stand)
                self.assertIn("independent test failed on earlier work, and the reviews passed on your change", text)
                self.assertIn(f"`{W.route_start_command(path, 'test', jobs='/jobs.log')}`", text)
                self.assertNotIn("Do not correct further", text)
        # a test that only died without a verdict is started again (a death spends no verdict round)
        text, path = self.wake(route, lineage={**{leg: [passed(leg, "d1")] for leg in legs}, "test": [dead]},
                               current="d1", reasons=stand)
        self.assertIn("Your route's independent test ended without a verdict.", text)
        self.assertIn(f"`{W.route_start_command(path, 'test', jobs='/jobs.log')}`", text)
        # one round each used and no report: correct once, the reviews first
        once = {**{leg: [passed(leg, "d1")] for leg in legs}, "test": [failed_test("d1", "t1")]}
        text, path = self.wake(route, lineage=once, current="d1", reasons=stand)
        self.assertIn("independent test fails on your current work", text)
        self.assertIn(f"`{W.route_start_command(path, 'impl-review', jobs='/jobs.log')}`", text)

    def test_a_refused_start_of_the_spent_test_ends_the_owner_and_never_points_inline(self):
        import review_round_cap as ROUND
        route = self.solo("strong")
        test = RF.one_shot_test_node(route)
        spent = ROUND.recovery_fields(test["kind"], "exhausted", route=route, node=test, jobs="/jobs.log",
                                      route_file="/route.json")
        self.assertEqual((spent["recovery_surface"], spent["next_action"]), ("owner-report", "owner-report"))
        self.assertIn("Do not correct further", spent["recovery_hint"])
        self.assertIn("`verdict: FAIL`", spent["recovery_hint"])
        bound = ROUND.recovery_fields(test["kind"], "verdictless-bound", route=route, node=test, jobs="/jobs.log",
                                      route_file="/route.json")
        for fields in (spent, bound):
            self.assertFalse(any("complete --inline" in str(value) for value in fields.values()), fields)
        # a team route's test keeps its own recovery
        team = self.compose(shape="staged", intensity="strong", graph="execute,impl-review,test,report", slug="s3-rf")
        team_test = next(n for n in team["nodes"] if n["id"] == "test")
        self.assertEqual(ROUND.recovery_fields(team_test["kind"], "exhausted", route=team, node=team_test,
                                               jobs="/jobs.log", route_file="/route.json")["recovery_surface"],
                         "capability-route complete --inline")

    def wake(self, route, reason="completion-marker-absent", lineage=None, fields=None, reasons=None, current="d1"):
        """The owner's wake text when its end lacks the verification nodes: every depth-2 node is missing for
        `reason`, except as `reasons` says (a node mapped to None stands)."""
        gaps = {n["id"]: reason for n in route["nodes"] if n.get("dispatch_depth") == 2}
        for key, value in (reasons or {}).items():
            if value is None:
                gaps.pop(key, None)
            else:
                gaps[key] = value
        path = self.route_file(route)
        row = SimpleNamespace(status="open", metadata={"attempt_id": "att-owner"})
        with mock.patch.object(JOIN, "exact_attempt_row", return_value=row), \
             mock.patch.object(DTC, "_completion_owner_choice", return_value=SimpleNamespace(allowed=True)), \
             mock.patch.object(DTC, "owner_workflow_gaps", return_value=gaps), \
             mock.patch.object(W, "_lineage_rows", return_value=lineage or {}), \
             mock.patch("review_source_subject.observe", return_value={"digest": current}), \
             mock.patch.object(W, "continuation_followup_fields",
                               side_effect=lambda _r, node, *a, **k: (fields or {}).get(node["id"], {})):
            return DTC.owner_workflow_continuation("/jobs.log", "att-owner", str(path)), path


if __name__ == "__main__":
    unittest.main()
