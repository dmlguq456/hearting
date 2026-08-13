#!/usr/bin/env python3
"""Hermetic unit tests — F-28b route-aware breadcrumb (render.py `_stage_segs`/
`_dispatch_stage_segs` and attached projection route sequences).

T2-1 is the regression pin: a record-less job's breadcrumb must be BYTE-IDENTICAL to the
pre-v10 `_PIPE_STAGES` path — route_seq is purely additive.
"""
import os
import sys
import tempfile
import unittest

_TOOLS_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)

from fleet import projection, render         # noqa: E402
from fleet import route                     # noqa: E402
from fleet.collectors import dispatch       # noqa: E402
from fleet.model import DispatchJob         # noqa: E402

_FIXDIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "route")
_REAL_CLAUDE = os.path.join(_FIXDIR, "real_claude_staged.json")
_REAL_RID = "rt-9fa0fed86699b8f5"
_LAB = os.path.join(_FIXDIR, "synth_parallel_lab.json")
_COMPOSED = os.path.join(_FIXDIR, "synth_composed_survey.json")


def _joined(lines):
    return "\n".join("".join(t for t, _k in ln) for ln in lines if ln)


class RouteBreadcrumbTest(unittest.TestCase):
    def setUp(self):
        route.clear_cache()
        self._saved_evidence = dispatch.collect.last_route_nodes
        dispatch.collect.last_route_nodes = {}

    def tearDown(self):
        dispatch.collect.last_route_nodes = self._saved_evidence
        route.clear_cache()

    def test_t2_1_record_less_job_matches_pre_v10_breadcrumb_exactly(self):
        # v16: "record-less" (no sealed route file) is not the same as "evidence-less" — the
        # WorkProjection resolver (projection.py) only recreates the pre-v10 fixed `_PIPE_STAGES`
        # breadcrumb from a real exact-cardinality artifact match (plan Step 1.3.7), never from
        # a bare/manually-set `stage` string with zero backing evidence. Give the job a real
        # exact plan-directory artifact so its compat `stage` is legitimately artifact-inferred,
        # and assert THAT breadcrumb is byte-identical to the pre-v10 `_PIPE_STAGES` path.
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = os.path.join(tmp, ".agent_reports", "plans", "2026-01-01_no-route-job",
                                    "execute")
            os.makedirs(plan_dir)
            job = DispatchJob(key="code", stage="exec", slug="no-route-job", cwd=tmp,
                              liveness="working", depth=1)
            via_build_lines = _joined(
                render._build_lines([], [job], section="dispatch", narrow=False,
                                    malformed=0, layout="wide"))
        direct = render._dispatch_stage_segs(job, "code", "exec", "no-route-job", working=True)
        expected_text = "".join(t for t, _k in direct)
        self.assertIn(expected_text, via_build_lines)
        # explicit sanity: the OLD 3-stage `_PIPE_STAGES` vocabulary is what rendered — not a
        # record node id (there is no sealed route record here at all).
        self.assertIn("exec", expected_text)

    def test_t2_1b_zero_evidence_job_shows_preparing_not_a_fabricated_stage(self):
        # Companion case: a job with NEITHER a route record NOR any resolvable artifact/registry
        # evidence must not have its manually-set `stage` echoed back — WorkProjection resolves
        # source="none" and the compat `stage` field clears, so the breadcrumb shows one honest
        # preparation state instead of fabricating either a lit "exec" or a fixed stage track.
        job = DispatchJob(key="code", stage="exec", slug="no-route-job", cwd="/nonexistent/x",
                          liveness="working", depth=1)
        via_build_lines = _joined(
            render._build_lines([], [job], section="dispatch", narrow=False,
                                malformed=0, layout="wide"))
        self.assertIn("preparing…", via_build_lines)
        self.assertNotIn("pre › plan › exec › test", via_build_lines)

    def test_t2_2_real_record_lights_active_child_node(self):
        dispatch.collect.last_route_nodes = {
            _REAL_RID: {"plan": {"status": "done", "slug": "v10-plan", "ts": None,
                                 "note": None, "model": "opus", "harness": "claude",
                                 "effort": "high", "completion_gate": "code-plan"}},
        }
        conductor = DispatchJob(key="code", slug="v10-conductor", cwd="/x", depth=1,
                                liveness="working", capability_owner="autopilot-code")
        child = DispatchJob(key="code-execute", slug="v10-conductor-execute", cwd="/x",
                            parent_slug="v10-conductor", depth=2, liveness="working",
                            route_id=_REAL_RID, route_file=_REAL_CLAUDE, route_node="execute")
        text = _joined(render._build_lines([], [conductor, child], section="dispatch",
                                           narrow=False, malformed=0, layout="wide"))
        # _STAGE_ZONE_MAX(30) crops PAST stages first (SD-F2, unchanged _drop_past_stages) —
        # "plan✓" folds away here exactly like a 4-stage `_PIPE_STAGES` breadcrumb would, so
        # what survives is the active node onward, using the RECORD's own node id ("execute",
        # not the `_PIPE_STAGES` "exec" abbreviation).
        self.assertIn("execute", text)
        self.assertIn("test", text)
        self.assertIn("report", text)
        # the underlying route_seq itself DOES carry plan as done — verified unit-level,
        # independent of the width crop above.
        views = route.build_views([child], dispatch.collect.last_route_nodes,
                                  {_REAL_RID: route.load(_REAL_CLAUDE)}, now=0.0)
        plan_node = next(n for n in views[0]["nodes"] if n["id"] == "plan")
        self.assertEqual(plan_node["state"], "done")

    def test_t2_3_non_pipe_stages_node_labels_render_without_crashing(self):
        dispatch.collect.last_route_nodes = {}
        lab_rid = "rt-6f5423d05eaf3189"
        conductor = DispatchJob(key="lab", slug="lab-conductor", cwd="/x", depth=1,
                                liveness="working")
        child = DispatchJob(key="lab-eval", slug="lab-conductor-eval-sep", cwd="/x",
                            parent_slug="lab-conductor", depth=2, liveness="working",
                            route_id=lab_rid, route_file=_LAB, route_node="eval-sep")
        text = _joined(render._build_lines([], [conductor, child], section="dispatch",
                                           narrow=False, malformed=0, layout="wide"))
        self.assertIn("eval-sep", text)

    def test_t2_4_record_with_zero_live_children_stays_unlit(self):
        # SD-F2 — record presence alone never lights a node; only live evidence does.
        dispatch.collect.last_route_nodes = {}
        conductor = DispatchJob(key="code", slug="v10-idle-conductor", cwd="/x", depth=1,
                                liveness="idle")
        child = DispatchJob(key="code-plan", slug="v10-idle-conductor-plan", cwd="/x",
                            parent_slug="v10-idle-conductor", depth=2, liveness="idle",
                            route_id=_REAL_RID, route_file=_REAL_CLAUDE, route_node="plan")
        views = route.collect_views([child], dispatch.collect.last_route_nodes)
        self.assertEqual(views[0]["nodes"][0]["state"], "pending")

    def test_t2_5_narrow_width_overflow_zero(self):
        node_view = [(n, "pending") for n in ("plan", "execute", "test", "report")]
        segs = render._stage_segs("code", "", working=False,
                                  max_width=render._STAGE_ZONE_MAX, route_seq=node_view)
        width = sum(render._dw(t) for t, _k in segs)
        self.assertLessEqual(width, render._STAGE_ZONE_MAX)

    def test_t2_6_autopilot_spec_research_node_renders(self):
        segs = render._dispatch_stage_segs(
            DispatchJob(key="spec-research", slug="v94-note-db-steward-research", depth=1),
            "spec-research", "research", "v94-note-db-steward-research", working=True,
            route_seq=[("research", "active")])
        text = "".join(t for t, _k in segs)
        self.assertEqual(text, "research")

    def test_t2_7_composed_survey_breadcrumb_keeps_parallel_siblings(self):
        record = route.load(_COMPOSED)
        rid = record["route_id"]
        owner = DispatchJob(key="survey", slug="composed-owner", depth=1,
                            cwd="/composed", liveness="working")
        child_b = DispatchJob(key="claim", slug="claim-b", parent_slug="composed-owner",
                              depth=2, cwd="/composed/b", liveness="working",
                              route_id=rid, route_file=_COMPOSED, route_node="claim-b",
                              assigned_contract="autopilot-code")
        child_a = DispatchJob(key="claim", slug="claim-a", parent_slug="composed-owner",
                              depth=2, cwd="/composed/a", liveness="working",
                              route_id=rid, route_file=_COMPOSED, route_node="claim-a",
                              assigned_contract="autopilot-code")
        projection.attach_projections([], [owner, child_b, child_a], now=100.0)
        text = _joined(render._build_lines([], [owner, child_b, child_a], section="dispatch",
                                           narrow=False, malformed=0, layout="wide",
                                           term_width=168))
        self.assertIn("claim-a", text)
        self.assertIn("claim-b", text)
        self.assertIn("survey › claim-a › claim-b › synth", text)
        self.assertNotIn("pre › plan › exec › test", text)


class ReplicaCollapseTest(unittest.TestCase):
    """2026-07-24: replica legs (shared replica_group) fold into one `<group>(N-way)`
    token on every route-projection surface — the legs keep their own dispatch rows."""

    @staticmethod
    def _nodes(primary_state="active", replica_state="active"):
        return [
            {"id": "execute", "state": "done", "level": 0, "depends_on": [],
             "replica_group": None},
            {"id": "impl-review", "state": primary_state, "level": 1,
             "depends_on": ["execute"], "replica_group": "impl-review"},
            {"id": "impl-review-replica", "state": replica_state, "level": 1,
             "depends_on": ["execute"], "replica_group": "impl-review"},
            {"id": "test", "state": "pending", "level": 2,
             "depends_on": ["impl-review", "impl-review-replica"],
             "replica_group": None},
        ]

    def test_record_view_carries_replica_group(self):
        record = {"nodes": [
            {"id": "execute", "depends_on": []},
            {"id": "impl-review", "depends_on": ["execute"],
             "replica_group": "impl-review"},
            {"id": "impl-review-replica", "depends_on": ["execute"],
             "replica_group": "impl-review"},
        ]}
        view = route._record_view(record, "rt-x", [], {}, 100.0)
        groups = {n["id"]: n.get("replica_group") for n in view["nodes"]}
        self.assertEqual(groups["impl-review"], "impl-review")
        self.assertEqual(groups["impl-review-replica"], "impl-review")
        self.assertIsNone(groups["execute"])

    def test_collapse_merges_legs_and_rewrites_downstream_deps(self):
        collapsed = render._collapse_replica_nodes(self._nodes())
        ids = [n["id"] for n in collapsed]
        self.assertEqual(ids, ["execute", "impl-review(2-way)", "test"])
        merged = collapsed[1]
        self.assertEqual(merged["state"], "active")
        self.assertEqual(merged["depends_on"], ["execute"])
        self.assertEqual(collapsed[2]["depends_on"], ["impl-review(2-way)"])

    def test_canonical_parallel_group_collapses_three_asymmetric_legs(self):
        nodes = [
            {"id": "frame", "state": "active", "level": 0, "depends_on": [],
             "parallel_group": "frame", "model_profile": "balanced-deep"},
            {"id": "frame-alternative", "state": "active", "level": 0, "depends_on": [],
             "parallel_group": "frame", "model_profile": "light"},
            {"id": "frame-contrarian", "state": "active", "level": 0, "depends_on": [],
             "parallel_group": "frame", "model_profile": "deep"},
            {"id": "plan", "state": "pending", "level": 1,
             "depends_on": ["frame", "frame-alternative", "frame-contrarian"]},
        ]
        collapsed = render._collapse_parallel_nodes(nodes)
        self.assertEqual([node["id"] for node in collapsed], ["frame(3-way)", "plan"])
        self.assertEqual(collapsed[1]["depends_on"], ["frame(3-way)"])

    def test_collapse_state_is_strictest_of_the_group(self):
        cases = (
            (("failed", "active"), "failed"),
            (("done", "done"), "done"),
            (("pending", "pending"), "pending"),
            (("done", "active"), "active"),
            (("done", "pending"), "done"),
            (("reconciling", "done"), "reconciling"),
            (("degraded", "reconciling"), "degraded"),
        )
        for (primary, replica), expected in cases:
            collapsed = render._collapse_replica_nodes(
                self._nodes(primary_state=primary, replica_state=replica))
            with self.subTest(primary=primary, replica=replica):
                self.assertEqual(collapsed[1]["state"], expected)

    def test_singleton_group_and_plain_nodes_pass_through(self):
        nodes = [
            {"id": "impl-review", "state": "active", "level": 0, "depends_on": [],
             "replica_group": "impl-review"},
            {"id": "test", "state": "pending", "level": 1,
             "depends_on": ["impl-review"], "replica_group": None},
        ]
        self.assertEqual(render._collapse_replica_nodes(nodes), nodes)

    def test_inline_marker_gate_does_not_blank_the_group_gate(self):
        # S-2 additional surface: marker resolution gives an inline fallback
        # the same typed `gate_passed` carrier as registered legs.  Collapse
        # must preserve that carrier without consulting status-note strings.
        nodes = [
            {"id": "frame", "state": "done", "level": 0, "depends_on": [],
             "parallel_group": "frame", "gate_passed": True},
            {"id": "frame-alternative", "state": "done", "level": 0, "depends_on": [],
             "parallel_group": "frame", "gate_passed": True},
            {"id": "frame-contrarian", "state": "done", "level": 0, "depends_on": [],
             "parallel_group": "frame", "gate_passed": True,
             "execution_surface": "inline"},
        ]
        collapsed = render._collapse_parallel_nodes(nodes)
        self.assertEqual(collapsed[0]["id"], "frame(3-way)")
        self.assertTrue(collapsed[0]["gate_passed"])


if __name__ == "__main__":
    unittest.main()
