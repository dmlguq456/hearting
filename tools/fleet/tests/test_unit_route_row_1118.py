"""11-18 unit route row: a route is chained only to routes its own records tie to it; a unit route reads its field alone (`code`).

  * `_chain_legs` chains a route to the route it continues (`source_route_id`), the legs of the plan it belongs to (`route_plan`)
    and the frame that opened that plan, or the steps of a declared `--plan`; a route that only sits in the same session
    or campaign stands alone. A unit route is nobody's leg and carries none. One tie needs no record: a route opened right
    after a frame route (unit routes skipped, same session and campaign) is that frame's.
  * The panel row and Fleet's PROGRESS cell lead a unit route with its unit's short name; the worker line names no role.
  * Fleet's unfolded row and the route page word a unit route by its unit (no owner, no mode, no reviewer, a named gate).
Everything is synthetic.
"""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../../run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fleet import route_chain, route_visibility, session_panel  # noqa: E402
from fleet.explorer import live, tables  # noqa: E402
from fleet.explorer.grid import CellGrid  # noqa: E402
from fleet.explorer.frame import _scope_title, draw  # noqa: E402
from fleet.explorer.state import NavState  # noqa: E402
from fleet.explorer.view_model import ExplorerViewModel  # noqa: E402

PLAN = {"decision": "frame/route-decision.json", "digest": "sha256:" + "a" * 64}


def node(rid, capability, state="done", **extra):
    return {"route_id": rid, "capability": capability, "state": state, **extra}


def legs(nodes, rid, own, status="active"):
    return live._chain_legs(nodes, rid, own, status, None)


class ChainedByRecords(unittest.TestCase):
    def test_routes_of_one_session_that_no_record_ties_stand_alone(self):
        chain = [node("c0", "autopilot-code"), node("u1", "unit", "open"), node("c1", "autopilot-code", "open")]
        self.assertEqual(legs(chain, "u1", "unit"), [["unit", "active", None]])
        self.assertEqual(legs(chain, "c1", "code"), [["code", "active", None]])

    def test_a_frame_leads_the_leg_of_its_plan_and_the_legs_chain_to_each_other(self):
        chain = [node("f1", "route-frame"), node("c1", "autopilot-code", route_plan={**PLAN, "index": 0}),
                 node("r1", "autopilot-research", "open", route_plan={**PLAN, "index": 1})]
        self.assertEqual(legs(chain, "c1", "code"), [["frame", "done", None], ["code", "active", None]])
        self.assertEqual(legs(chain, "r1", "research"),
                         [["frame", "done", None], ["code", "done", None], ["research", "active", None]])

    def test_a_route_of_another_plan_or_with_none_is_not_in_the_chain(self):
        other = {**PLAN, "digest": "sha256:" + "b" * 64}
        chain = [node("f1", "route-frame"), node("c1", "autopilot-code", route_plan={**PLAN, "index": 0}),
                 node("c2", "autopilot-code", "open", route_plan={**other, "index": 1}), node("c3", "autopilot-code", "open")]
        self.assertEqual(legs(chain, "c2", "code")[:-1], [["frame", "done", None]])      # (its own frame: the nearest before it)
        self.assertEqual(legs(chain, "c3", "code"), [["code", "active", None]])

    def test_a_continuation_chains_to_the_route_it_continues(self):
        chain = [node("c1", "autopilot-code"), node("x1", "autopilot-lab"),
                 node("r2", "autopilot-research", "open", source_route_id="c1")]
        self.assertEqual(legs(chain, "r2", "research"), [["code", "done", None], ["research", "active", None]])

    def test_declared_plan_steps_chain_and_a_unit_route_never_does(self):
        chain = [node("r1", "autopilot-research", planned=True), node("u1", "unit"), node("d1", "autopilot-draft", "open", planned=True)]
        self.assertEqual(legs(chain, "d1", "draft"), [["research", "done", None], ["draft", "active", None]])
        planned_unit = [node("r1", "autopilot-research", planned=True), node("u1", "unit", "open", planned=True)]
        self.assertEqual(legs(planned_unit, "u1", "unit"), [["unit", "active", None]])
        self.assertEqual(legs([node("u1", "unit", route_plan={**PLAN, "index": 0}),
                               node("c1", "autopilot-code", "open", route_plan={**PLAN, "index": 1})], "c1", "code"),
                         [["code", "active", None]])

    def test_a_route_opened_by_hand_right_after_a_frame_is_that_frames(self):
        # A frame that ends with nothing to choose leaves the next route to be composed by hand: no plan, no `source_route_id`.
        chain = [node("f1", "route-frame"), node("c1", "autopilot-code", "open")]
        self.assertEqual(legs(chain, "c1", "code"), [["frame", "done", None], ["code", "active", None]])
        # A frame that has not ended keeps its own form.
        self.assertEqual(legs([node("f1", "route-frame", "active"), node("c1", "autopilot-code", "open")], "c1", "code"),
                         [["frame", "active", None], ["code", "active", None]])
        # The route opened after that one stands alone: it is not right after the frame.
        chain.append(node("c2", "autopilot-code", "open"))
        self.assertEqual(legs(chain, "c2", "code"), [["code", "active", None]])
        # A unit route in between is a side branch: the first route is still the frame's, the unit route stands alone.
        chain = [node("f1", "route-frame"), node("u1", "unit"), node("c1", "autopilot-code", "open")]
        self.assertEqual(legs(chain, "c1", "code"), [["frame", "done", None], ["code", "active", None]])
        self.assertEqual(legs(chain, "u1", "unit"), [["unit", "active", None]])
        # A frame route is the frame's only by a record; so is a unit route never.
        self.assertEqual(legs([node("f1", "route-frame"), node("f2", "route-frame", "active")], "f2", "frame", "active"),
                         [["frame", "active", None]])
        self.assertEqual(legs([node("f1", "route-frame"), node("u1", "unit", "open")], "u1", "unit"), [["unit", "active", None]])
        # A route that names no route id is not the one a frame with no route id is "right before": `None` is no frame.
        self.assertFalse(live._chain_links({"capability": "autopilot-code"}, {"capability": "route-frame"}, {}))

    def test_a_route_of_another_campaign_is_not_the_frames(self):
        def line(rid, capability, key, ts):
            return {"route_id": rid, "capability": capability, "route_file": "f", "route_hash": "h", "ts": ts, "event": "compose",
                    "campaign_key": key}
        for second, tied in (("camp-a", True), ("camp-b", False)):
            lines = [line("f1", "route-frame", "camp-a", 1.0), line("c1", "autopilot-code", second, 2.0)]
            nodes = route_chain.assemble(lines, load_record=lambda *a: None, load_outcome=lambda *a: None)["nodes"]
            names = [leg[0] for leg in live._chain_legs(nodes, "c1", "code", "active", None)]
            self.assertEqual(names, ["frame", "code"] if tied else ["code"], second)

    def test_a_frame_opened_between_the_legs_of_a_plan_is_not_the_frame_of_the_later_leg(self):
        chain = [node("fA", "route-frame"), node("c1", "autopilot-code", route_plan={**PLAN, "index": 0}),
                 node("fB", "route-frame", "active"), node("r1", "autopilot-research", "open", route_plan={**PLAN, "index": 1})]
        self.assertEqual(legs(chain, "r1", "research"),
                         [["frame", "done", None], ["code", "done", None], ["research", "active", None]])

    def test_the_ledger_node_carries_what_the_chain_reads(self):
        record = {"route_id": "c1", "capability": "autopilot-code", "route_plan": {**PLAN, "index": 0, "extra": 1}}
        line = {"route_id": "c1", "capability": "autopilot-code", "route_file": "f", "route_hash": "h", "ts": 1.0,
                "event": "compose"}
        chain = route_chain.assemble([line], load_record=lambda *a: record, load_outcome=lambda *a: None)
        first = chain["nodes"][0]
        self.assertEqual(first["route_plan"], {**PLAN, "index": 0})


class UnitRouteWords(unittest.TestCase):
    def unit(self, **extra):
        return {"capability": "unit", "intensity": "quick", "worker_unit": "qa/code-review", "state": "working",
                "shape": "one-shot-owner", **extra}

    def cell(self, route, width):
        grid = CellGrid(width, 1)
        grid.beat = 1
        tables._draw_route_progress(grid, 0, width, 0, {**route, "stages": [{"name": "unit", "st": "active"}],
                                                        "legs": (("unit", "active", None),)}, card=False)
        return "".join(c[0] for c in grid.cells[0]).rstrip()

    def test_the_progress_cell_leads_with_the_field_and_has_no_bracket(self):
        self.assertEqual(self.cell(self.unit(field="code"), 40), "code")
        self.assertEqual(self.cell(self.unit(field="docs"), 10), "docs")
        self.assertEqual(self.cell(self.unit(field="research", worker_unit="research/research-survey"), 40), "research")
        self.assertEqual(self.cell(self.unit(field="code", state="done"), 40), "code")   # (the `success · title` line is the one left as it was)
        self.assertEqual(self.cell(self.unit(field="plan"), 40), "plan")
        # An older unit route records no field: `unit`, still with no bracket and no unit name.
        self.assertEqual(self.cell(self.unit(), 40), "unit")
        # A route with an owner keeps its bracket.
        self.assertEqual(self.cell({**self.unit(), "capability": "code", "worker_unit": "dev/backend"}, 40), "code(quick)")
        self.assertEqual(tables._kind_cell(self.unit(field="code")), [("code", "kind_code")])

    def test_the_panel_row_names_the_unit_and_the_field(self):
        row = {"capability": "unit", "worker_unit": "qa/code-review", "marker": "working", "field": "code",
               "intensity": "quick"}
        shown = session_panel.route_row(row)
        self.assertEqual((shown["kind_word"], shown["kind_field"], shown["unit_word"], shown["unit_route"]),
                         ("code", "code", "code-review", True))
        kind = session_panel.route_kind({**row, "chain": [["unit", "active"]]})
        self.assertNotIn("intensity", kind)             # (a unit route's `quick` is no choice: no bracket)
        self.assertNotIn("chain", kind)                 # (and it is nobody's leg)
        self.assertEqual(session_panel.route_kind({**row, "capability": "code"})["intensity"], "quick")
        plan = session_panel.route_row({**row, "field": "plan"})
        self.assertEqual(plan["kind_word"], "plan")
        self.assertNotIn("kind_field", plan)
        old = session_panel.route_row({**row, "field": ""})
        self.assertEqual((old["kind_word"], old["unit_word"]), ("unit", "code-review"))
        other = session_panel.route_row({**row, "capability": "code"})
        self.assertNotIn("unit_word", other)
        self.assertNotIn("unit_route", other)

    def test_the_fleet_row_and_tree_name_a_unit_route_by_its_field_and_its_unit(self):
        rid, repo, now = "rt-unit", "/synthetic/repo", 1_000_000.0
        view = {"nodes": [{"id": "unit", "state": "active", "depends_on": [], "unit": "qa/code-review", "worker_type": "worker"}],
                "capability": "unit", "field": "code", "execution_topology": "one-shot-owner", "effective_intensity": "quick",
                "cwd": repo, "slug": "review subject"}
        record = {"route_id": rid, "capability": "unit", "field": "code", "execution_topology": "one-shot-owner",
                  "selection": {"shape": "solo"}, "effective_intensity": "quick", "owner_attempt_id": "att-u1", "result": None,
                  "slug": "review subject"}
        job = {"key": "route-unit", "depth": 1, "dispatch_depth": 1, "route_node": "unit", "worker_type": "worker",
               "worker_mode": "qa/code-review", "unit": "qa/code-review", "liveness": "working", "harness": "claude",
               "model": "opus", "resolved_model": None, "effort": "xhigh", "route_id": rid, "attempt_id": "att-u1", "elapsed_min": 3}
        session = {"harness": "claude", "session_id": "sid-u", "session_tag": "u1", "cwd": repo, "liveness": "idle",
                   "is_child": False, "title": "owner"}
        with patch.object(live, "_collect_routes", return_value={rid: {"view": view, "record": record, "jobs": [job],
                                                                       "sessions": [session]}}):
            rows, index = live._session_records({"sessions": [session]}, now, lambda _cwd: "repo")
            routes, nodes = live._route_records({"resources": []}, rows, index, lambda _cwd: "repo", now)
        route = routes[0]
        self.assertEqual((route["capability"], route["tree_label"]), ("unit", "code"))      # the session tree reads `code:`
        vm = ExplorerViewModel.from_mapping({"repos": [{"id": "repo", "name": "repo"}], "sessions": rows, "routes": routes,
                                             "nodes": nodes})
        title = _scope_title(vm, NavState(view="ROUTES", scope="route:" + rid))
        self.assertIn("code · ", title)                                                      # the Enter page's title
        self.assertNotIn("unit", title)
        opened = draw(vm, NavState(view="ROUTES", title="ROUTES", folds=(("ropen:ROUTES", rid),)), 180, 60)
        pane = ["".join(cell[0] for cell in row[34:]) for row in opened.cells]
        kind = next(line for line in pane if "└ kind" in line)
        self.assertIn("└ kind code", kind)
        self.assertNotIn("owner", kind)                                                      # no owner on a unit route
        self.assertTrue(any("└ unit · qa/code-review" in line for line in pane), pane[:14])
        self.assertFalse(any("└ worker" in line for line in pane))

    def test_the_route_page_names_the_worker_where_a_unit_route_has_no_owner(self):
        record = {"capability": "unit"}
        ended = {"liveness": "done", "attempt_id": "att-u4", "reason": "done"}
        verification = {"closed_at": None, "terminal_gate_proven": False}
        said = route_visibility._next_step(record, [], verification, None, ended, None, None)
        self.assertEqual(said["text"], "worker att-u4 ended (done) · route not closed")
        running = {"liveness": "working", "harness": "claude", "started_at": None}
        said = route_visibility._next_step(record, [], verification, None, running, None, None)
        self.assertEqual(said["text"], "wait · worker claude working")
        # Another route keeps `owner`.
        for capability in ("autopilot-code", "code"):
            other = {"capability": capability}
            self.assertEqual(route_visibility._next_step(other, [], verification, None, ended, None, None)["text"],
                             "owner att-u4 ended (done) · route not closed")
            self.assertEqual(route_visibility._next_step(other, [], verification, None, running, None, None)["text"],
                             "wait · owner claude working")

    def test_the_route_page_words_a_unit_route_by_its_unit(self):
        desc = {"capability": "unit", "intensity": "quick", "shape": "solo", "basis": {"kind": "default"},
                "stages": [{"id": "unit", "unit": "qa/code-review", "state": "active", "role": "worker", "gate_passed": False}],
                "current": "unit", "current_state": "active", "owner": None,
                "verification": {"completion_gates": [{"node": "unit", "gate": "unit-complete", "passed": None}], "reviewers": [],
                                 "ci": []},
                "links": [], "next": {"text": "wait"}}
        labels = [label for label, _text, _tone in route_visibility.detail_lines(desc)]
        self.assertEqual(labels[0], "unit")
        self.assertNotIn("owner", labels)
        self.assertNotIn("review", labels)
        self.assertEqual(route_visibility.detail_lines(desc)[0][1], "qa/code-review")
        self.assertEqual(route_visibility.summary_text(desc, current=False), "gate unit-complete")
        # An owner route keeps every word it had.
        code = {**desc, "capability": "autopilot-code", "shape": "solo"}
        self.assertTrue(route_visibility.summary_text(code, current=False).startswith("solo·default"))
        self.assertIn("owner", [label for label, _t, _tone in route_visibility.detail_lines(code)])


if __name__ == "__main__":
    unittest.main()
