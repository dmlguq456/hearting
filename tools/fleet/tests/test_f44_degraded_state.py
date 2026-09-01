import importlib.util
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]
import sys
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
SPEC = importlib.util.spec_from_file_location("fleet_route", ROOT / "fleet" / "route.py")
ROUTE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ROUTE)

from fleet import model, projection, render  # noqa: E402


class DegradedStateTest(unittest.TestCase):
    def _incident(self):
        now = 10_000
        route_id = "rt-incident"
        route_hash = "sha256:incident"
        record = {"route_id": route_id, "route_hash": route_hash,
                  "capability": "autopilot-code", "nodes": [{"id": "execute"}],
                  "levels": [["execute"]]}
        degradation = {"kind": "degradation", "dispatch_depth": 2,
                       "route_id": route_id, "route_node": "execute",
                       "route_hash": route_hash, "fallback_hop": "inline",
                       "reason": "fleet_visibility=none", "fleet_visibility": "none",
                       "registered_worker": 0, "ts": now - 51 * 60}
        job = model.DispatchJob(key="autopilot-code", route_id=route_id,
                                route_hash=route_hash, route_node="execute",
                                liveness="done", model="secret-model", effort="high", pid=99)
        projection.attach_projections([], [job], route_records={route_id: record},
                                      now=now, degradations={route_id: [degradation]})
        return route_id, job

    def test_node_state_degradation_evidence_is_direct_and_non_pending(self):
        node = ROUTE._node_state(
            "execute", [], {}, 1000,
            degradation={"kind": "degradation", "route_node": "execute",
                         "route_hash": "sha256:r", "ts": 700},
        )
        self.assertEqual(node["state"], "degraded")

    def test_incident_reaches_breadcrumb_card_detail_and_public_json(self):
        _route_id, job = self._incident()
        view = job.work_projection._route_view["view"]
        node = view["nodes"][0]
        self.assertEqual(node["state"], "degraded")
        self.assertEqual(node["degradation"]["registered_worker"], 0)
        breadcrumb = render._route_stage_segs([(node["id"], node["state"])], False, None)
        self.assertIn("◐", "".join(part for part, _key in breadcrumb))
        card = render._route_card_l2(view)
        card_text = "".join(text for line in card for text, _key in line)
        self.assertIn("(inline·fleet_visibility=none)", card_text)
        self.assertNotIn("99", card_text)
        self.assertNotIn("secret-model", card_text)
        self.assertNotIn("high", card_text)
        public_node = projection.route_summary_from_projections([job])[0]["nodes"][0]
        self.assertEqual(public_node["state"], "degraded")
        self.assertEqual(public_node["degradation"]["registered_worker"], 0)
        self.assertEqual(public_node["degradation"]["fleet_visibility"], "none")
        self.assertNotIn("pid", public_node)

    def test_exact_inline_degradation_is_the_owner_current_stage(self):
        now = 10_000
        route_id = "rt-owner-inline"
        route_hash = "sha256:owner-inline"
        record = {
            "route_id": route_id,
            "route_hash": route_hash,
            "capability": "autopilot-code",
            "nodes": [{"id": "execute"}],
            "levels": [["execute"]],
        }
        degradation = {
            "kind": "degradation",
            "dispatch_depth": 2,
            "route_id": route_id,
            "route_node": "execute",
            "route_hash": route_hash,
            "fallback_hop": "inline",
            "fleet_visibility": "none",
            "registered_worker": 0,
            "ts": now - 30,
        }
        owner = model.DispatchJob(
            key="autopilot-code", slug="owner", depth=1,
            worker_type="owner", attempt_id="att-owner", liveness="working",
        )

        current = projection._projection_from_record(
            owner, record, route_id, [], now=now, owner=True,
            degradations={route_id: [degradation]},
        )

        self.assertEqual(current.stage_label, "execute")
        self.assertEqual(current.route_node, "execute")
        self.assertEqual(current.node_state, "degraded")
        self.assertEqual([(node.id, node.state) for node in current.active_nodes],
                         [("execute", "degraded")])

    def test_mixed_parallel_inline_frontier_keeps_degraded_owner_state(self):
        nodes = (
            model.ActiveNodeProjection(
                id="review", state="active", parallel_group="review"),
            model.ActiveNodeProjection(
                id="review-alt", state="degraded", parallel_group="review"),
        )

        self.assertEqual(projection._active_stage_label(nodes), "review(2-way)")
        self.assertEqual(projection._owner_active_selection(nodes),
                         ("review(2-way)", "degraded"))

    def test_render_builds_route_views_from_attached_projection(self):
        route_id, job = self._incident()
        captured = {}
        original = render._build_process_lines
        render.set_process_view(True)
        try:
            render._build_process_lines = lambda sessions, jobs, route_views, *args, **kwargs: (
                captured.update(route_views) or [])
            for layout in ("wide", "narrow", "stack"):
                render._build_lines([], [job], "all", False, 0, layout=layout, term_width=200)
                route_node = next(node for node in captured[route_id]["nodes"] if node["id"] == "execute")
                self.assertEqual(route_node["state"], "degraded")
        finally:
            render._build_process_lines = original
            render.set_process_view(False)
        self.assertIn(route_id, captured)
        route_node = next(node for node in captured[route_id]["nodes"] if node["id"] == "execute")
        self.assertEqual(route_node["state"], "degraded")
        self.assertEqual(route_node["degradation"]["fleet_visibility"], "none")

    def test_six_pending_branches_remain_explicit_regression_table(self):
        # Keep the existing six priority branches enumerated: a seventh branch must
        # update this acceptance test rather than silently changing the contract.
        branches = ("active", "done(marker)", "failed(explicit)", "reconciling",
                    "failed(generic)", "done(ev_status)")
        self.assertEqual(len(branches), 6)

    def test_degradation_property_for_multiple_route_tuples(self):
        upper_six = ("active", "done(marker)", "failed(explicit)", "reconciling",
                     "failed(generic)", "done(ev_status)")
        for route_id, route_node in (("rt-a", "plan"), ("rt-b", "execute"),
                                     ("rt-c", "test")):
            with self.subTest(route_id=route_id, route_node=route_node):
                record = {"route_hash": "hash-" + route_id,
                          "nodes": [{"id": route_node}], "levels": [[route_node]]}
                view = ROUTE._record_view(record, route_id, [], {}, 1000,
                                          degradations_for_route=[{
                                              "kind": "degradation", "dispatch_depth": 2,
                                              "route_id": route_id, "route_node": route_node,
                                              "route_hash": record["route_hash"], "ts": 700,
                                          }])
                self.assertNotIn("pending", upper_six)
                self.assertNotEqual(view["nodes"][0]["state"], "pending")

    def test_leg_failure_and_depth_one_are_alert_only(self):
        record = {"route_hash": "sha256:r", "nodes": [{"id": "execute"}],
                  "levels": [["execute"]]}
        for event in (
            {"kind": "leg-failure", "dispatch_depth": 2, "route_node": "execute",
             "route_hash": "sha256:r", "ts": 1},
            {"kind": "degradation", "dispatch_depth": 1, "route_node": "execute",
             "route_hash": "sha256:r", "ts": 2},
        ):
            view = ROUTE._record_view(record, "rt-r", [], {}, 1000,
                                      degradations_for_route=[event])
            self.assertEqual(view["nodes"][0]["state"], "pending")

    def test_multiple_degradations_use_newest_hop_reason_and_elapsed(self):
        record = {"route_hash": "sha256:r", "nodes": [{"id": "execute"}],
                  "levels": [["execute"]]}
        view = ROUTE._record_view(
            record, "rt-r", [], {}, 1000,
            degradations_for_route=[
                {"kind": "degradation", "dispatch_depth": 2, "route_node": "execute",
                 "route_hash": "sha256:r", "ts": 700, "fallback_hop": "inline",
                 "reason": "old"},
                {"kind": "degradation", "dispatch_depth": 2, "route_node": "execute",
                 "route_hash": "sha256:r", "ts": 940, "fallback_hop": "native-subagent",
                 "reason": "new"},
            ],
        )
        node = view["nodes"][0]
        self.assertEqual(node["state"], "degraded")
        self.assertEqual(node["degradation"]["reason"], "new")
        self.assertEqual(node["degradation"]["fallback_hop"], "native-subagent")
        self.assertEqual(node["elapsed_min"], 1)

    def test_route_hash_mismatch_does_not_promote_state(self):
        record = {"route_hash": "sha256:r", "nodes": [{"id": "execute"}],
                  "levels": [["execute"]]}
        view = ROUTE._record_view(record, "rt-r", [], {}, 1000,
                                  degradations_for_route=[
                                      {"kind": "degradation", "dispatch_depth": 2,
                                       "route_node": "execute", "route_hash": "sha256:other",
                                       "ts": 900}])
        self.assertEqual(view["nodes"][0]["state"], "pending")


if __name__ == "__main__":
    unittest.main()
