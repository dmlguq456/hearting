"""Focused F-36 projection and sealed composed-DAG acceptance checks."""
import hashlib
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from fleet import projection, render, route  # noqa: E402
from fleet.collectors import dispatch  # noqa: E402
from fleet.model import DispatchJob, Session  # noqa: E402


FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "route")
COMPOSED = os.path.join(FIXTURES, "synth_composed_survey.json")
REAL = os.path.join(FIXTURES, "real_claude_staged.json")


class WorkProjectionTest(unittest.TestCase):
    def _lineage_record(self, cwd, attempt, generation, *, source=None,
                        first_node="execute", family="family-linear"):
        nodes = [{"id": first_node, "depends_on": []}]
        record = {
            "schema_version": 1,
            "nodes": nodes,
            "cwd": cwd,
            "capability": "autopilot-code",
            "capability_mode": "debug",
            "owner_attempt_id": attempt,
            "route_family_key": family,
            "advance_generation": generation,
            "reused_nodes": [],
        }
        if source is not None:
            reused = [{"node_id": "plan", "completion_marker": "plan.done"}]
            continuation_id = "cont-" + hashlib.sha256(
                (source["route_id"] + first_node).encode()
            ).hexdigest()[:32]
            edge = {
                "edge_version": 1,
                "operation": "continuation",
                "from_route_id": source["route_id"],
                "from_route_hash": source["route_hash"],
                "to_continuation_id": continuation_id,
                "source_verdict_preserved": True,
            }
            record.update({
                "source_route_id": source["route_id"],
                "source_route_hash": source["route_hash"],
                "continuation_id": continuation_id,
                "source_route_supersession": edge,
                "supersession_edges": [*(source.get("supersession_edges") or []), edge],
                "reused_nodes": reused,
                "source_evidence_digest": "sha256:" + hashlib.sha256(
                    json.dumps(
                        reused, ensure_ascii=False, sort_keys=True,
                        separators=(",", ":"),
                    ).encode()
                ).hexdigest(),
            })
        record["route_hash"] = route.route_hash(record)
        record["route_id"] = "rt-" + record["route_hash"].split(":", 1)[1][:16]
        return record

    def test_terminal_only_child_route_does_not_reattach_to_main_session(self):
        record = route.load(REAL)
        rid = record["route_id"]
        owner = Session(harness="codex", pid=90, proc_start="main-start",
                        cwd="/owner", slug="main", session_id="sid-main",
                        liveness="idle")
        evidence = {rid: {
            node["id"]: dict({
                "status": "done", "parent": owner.session_id,
                "route_file": REAL, "route_hash": record["route_hash"],
            }, **({"note": "dead-worker-fail"} if node["id"] == "execute" else {}))
            for node in record["nodes"]
        }}
        historical = route.build_views([], evidence, {rid: record}, 100.0)[0]
        self.assertEqual(
            next(node for node in historical["nodes"] if node["id"] == "execute")["state"],
            "failed",
        )
        projection.attach_projections(
            [owner], [], route_records={rid: record}, node_evidence=evidence,
            now=100.0,
        )
        self.assertEqual(owner.work_projection.source, "none")
        self.assertIsNone(owner.work_projection.route_id)
        render.set_process_view(False)
        rendered = "\n".join(
            "".join(token for token, _kind in line)
            for line in render._build_lines(
                [owner], [], section="fleet", narrow=False, malformed=0,
                layout="wide", term_width=168,
            )
            if line
        )
        self.assertIn("main", rendered)
        self.assertNotIn("stage ", rendered)
        self.assertNotIn("plan ✓", rendered)

    def test_session_owner_render_shows_all_parallel_siblings_in_sealed_order(self):
        record = route.load(COMPOSED)
        rid = record["route_id"]
        owner = Session(harness="claude", pid=100, proc_start="owner-start",
                        cwd="/home/proj", slug="main", session_id="sid-owner",
                        liveness="working", elapsed_min=12, branch="main")
        # Reverse collector/jobs input order deliberately.  The attached route view
        # must still expose the sealed record order claim-a, claim-b.
        jobs = [
            DispatchJob(key="claim", slug="claim-b", parent_sid="sid-owner", depth=2,
                        route_id=rid, route_file=COMPOSED, route_node="claim-b",
                        assigned_contract="autopilot-code", liveness="working",
                        harness="claude", pid=102, proc_start="child-b"),
            DispatchJob(key="claim", slug="claim-a", parent_sid="sid-owner", depth=2,
                        route_id=rid, route_file=COMPOSED, route_node="claim-a",
                        assigned_contract="autopilot-code", liveness="working",
                        harness="claude", pid=101, proc_start="child-a"),
        ]
        projection.attach_projections([owner], jobs, now=100.0)
        self.assertEqual(owner.work_projection.stage_label, "{claim-a,claim-b}")
        render.set_process_view(False)
        try:
            for width in (168, 120, 100, 60):
                lines = render._build_lines(
                    [owner], jobs, section="both", narrow=width < 70,
                    malformed=0, layout=render._layout_mode(width), term_width=width,
                )
                text = "\n".join(
                    "".join(token for token, _kind in line)
                    for line in lines if line
                ).replace("\x00", "")
                with self.subTest(width=width):
                    self.assertIn("claim-a", text)
                    self.assertIn("claim-b", text)
                    self.assertIn("{claim-a,claim-b}", text)
                    self.assertNotIn("stage autopilot-code", text)
        finally:
            render.set_process_view(False)

    def test_owner_stage_label_uses_node_ids_for_generic_single_and_parallel_children(self):
        rid = route.load(REAL)["route_id"]
        single_owner = DispatchJob(key="owner", slug="single-owner", depth=1,
                                   liveness="working")
        single = DispatchJob(
            key="code", slug="single-child", parent_slug="single-owner", depth=2,
            route_id=rid, route_file=REAL, route_node="execute",
            assigned_contract="autopilot-code", liveness="working")
        parallel_owner = DispatchJob(key="owner", slug="parallel-owner", depth=1,
                                     liveness="working")
        first = DispatchJob(
            key="code", slug="parallel-a", parent_slug="parallel-owner", depth=2,
            route_id=route.load(COMPOSED)["route_id"], route_file=COMPOSED,
            route_node="claim-a", assigned_contract="autopilot-code",
            liveness="working")
        second = DispatchJob(
            key="code", slug="parallel-b", parent_slug="parallel-owner", depth=2,
            route_id=route.load(COMPOSED)["route_id"], route_file=COMPOSED,
            route_node="claim-b", assigned_contract="autopilot-code",
            liveness="working")
        projection.attach_projections([], [single_owner, single, parallel_owner, second, first], now=100.0)
        self.assertEqual(single_owner.work_projection.stage_label, "execute")
        self.assertEqual(parallel_owner.work_projection.stage_label, "{claim-a,claim-b}")

    def test_sealed_composed_dag_preserves_opaque_siblings_fanin_and_scope(self):
        record = route.load(COMPOSED)
        rid = record["route_id"]
        jobs = [
            DispatchJob(key="claim", slug="claim-a", parent_slug="owner", depth=2,
                        route_id=rid, route_file=COMPOSED, route_node="claim-a",
                        liveness="working"),
            DispatchJob(key="claim", slug="claim-b", parent_slug="owner", depth=2,
                        route_id=rid, route_file=COMPOSED, route_node="claim-b",
                        liveness="working"),
        ]
        owner = DispatchJob(key="analyze", slug="owner", depth=1, liveness="working")
        projection.attach_projections([], jobs + [owner], now=100.0)
        self.assertEqual({n.id for n in owner.work_projection.active_nodes}, {"claim-a", "claim-b"})
        view = owner.work_projection._route_view["view"]
        levels = [[n["id"] for n in view["nodes"] if n["level"] == level]
                  for level in range(3)]
        self.assertEqual(levels[0], ["survey"])
        self.assertEqual(levels[1], ["claim-a", "claim-b"])
        self.assertEqual(levels[2], ["synth"])
        claim_a = next(n for n in view["nodes"] if n["id"] == "claim-a")
        self.assertEqual(claim_a["write_scope"], ["reviews/claim-a/**"])
        self.assertNotIn("plan", {n["id"] for n in view["nodes"]})

    def test_exact_route_beats_artifact_and_public_route_is_json_safe(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "plans", "2026-07-22_exact-job", "test"))
            entity = DispatchJob(key="code", slug="exact-job", cwd=tmp,
                                 route_id="rt-9fa0fed86699b8f5", route_file=REAL,
                                 route_node="execute", liveness="working")
            projection.attach_projections([], [entity], artifact_root=tmp, now=100.0)
            self.assertEqual(entity.work_projection.source, "route-exact")
            self.assertEqual(entity.work_projection.stage_label, "execute")
            payload = projection.route_summary_from_projections([entity])
            json.dumps(payload)
            self.assertEqual(payload[0]["nodes"][1]["id"], "execute")
            self.assertNotIn("job", payload[0]["nodes"][1])

    def test_invalid_explicit_route_fails_closed_over_artifact(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "plans", "2026-07-22_bad-job", "test"))
            entity = DispatchJob(key="code", slug="bad-job", cwd=tmp,
                                 route_id="rt-mismatch", route_file=REAL,
                                 route_node="execute", liveness="working")
            projection.attach_projections([], [entity], artifact_root=tmp, now=100.0)
            self.assertEqual(entity.work_projection.source, "registry-exact")
            self.assertEqual(entity.work_projection.ambiguity, "route-record-mismatch")
            self.assertNotEqual(entity.work_projection.source, "artifact-inferred")

    def test_artifact_cardinality_is_exact_and_stage_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "plans", "2026-07-22_unique-job", "execute"))
            entity = DispatchJob(key="code", slug="unique-job", cwd=tmp)
            projection.attach_projections([], [entity], artifact_root=tmp, now=100.0)
            self.assertEqual(entity.work_projection.source, "artifact-inferred")
            self.assertIsNone(entity.work_projection.route_id)
            self.assertIsNone(entity.work_projection.progress)
            os.makedirs(os.path.join(tmp, "plans", "2026-07-22_second_unique-job", "test"))
            self.assertEqual(len(projection.exact_artifact_candidates(entity, tmp)), 2)
            projection.attach_projections([], [entity], artifact_root=tmp, now=100.0)
            self.assertEqual(entity.work_projection.ambiguity, "multiple-artifact-plan-dirs")

    def test_owner_rejects_different_child_routes(self):
        other = route.load(REAL)
        rid = route.load(COMPOSED)["route_id"]
        first = DispatchJob(key="claim", slug="a", parent_slug="owner", depth=2,
                            route_id=rid, route_file=COMPOSED, route_node="claim-a",
                            liveness="working")
        second = DispatchJob(key="code", slug="b", parent_slug="owner", depth=2,
                             route_id=other["route_id"], route_file=REAL, route_node="execute",
                             liveness="working")
        owner = DispatchJob(key="analyze", slug="owner", depth=1, liveness="working")
        projection.attach_projections([], [first, second, owner], now=100.0)
        self.assertEqual(owner.work_projection.ambiguity, "multiple-owner-routes")

    def test_post_launch_attachment_projects_route_then_first_child_stage(self):
        with tempfile.TemporaryDirectory() as tmp:
            record = self._lineage_record(tmp, "att-owner", 0)
            owner = DispatchJob(
                key="autopilot-code", slug="owner", depth=1,
                worker_type="owner", attempt_id="att-owner", cwd=tmp,
                capability_mode="debug", liveness="working",
            )
            owner._registry_path = os.path.join(tmp, "jobs.log")
            binding = mock.Mock(
                route_file=os.path.join(tmp, "r0.json"),
                route_id=record["route_id"], route_hash=record["route_hash"],
            )
            fake = self._fake_owner_route_binding_module(
                binding, "owner-route-post-launch-attachment"
            )
            with mock.patch.object(
                projection, "_owner_route_binding_module", return_value=fake,
            ):
                projection.attach_projections(
                    [], [owner], route_records={record["route_id"]: record}, now=100.0,
                )
            self.assertEqual(owner.work_projection.source, "route-exact")
            self.assertEqual(owner.work_projection.route_id, record["route_id"])
            self.assertIsNone(owner.work_projection.stage_label)
            child = DispatchJob(
                key="code-execute", slug="execute", depth=2, parent_slug="owner",
                route_id=record["route_id"], route_hash=record["route_hash"],
                route_file=os.path.join(tmp, "r0.json"), route_node="execute",
                liveness="working",
            )
            with mock.patch.object(
                projection, "_owner_route_binding_module", return_value=fake,
            ):
                projection.attach_projections(
                    [], [owner, child], route_records={record["route_id"]: record},
                    now=100.0,
                )
            self.assertEqual(owner.work_projection.stage_label, "execute")

    def test_unbound_linear_lineage_collapses_to_current_generation(self):
        with tempfile.TemporaryDirectory() as tmp:
            attempt = "att-linear"
            r0 = self._lineage_record(tmp, attempt, 0, first_node="plan")
            r1 = self._lineage_record(tmp, attempt, 1, source=r0, first_node="test")
            r2 = self._lineage_record(tmp, attempt, 2, source=r1, first_node="execute")
            owner = DispatchJob(
                key="autopilot-code", slug="owner", depth=1,
                worker_type="owner", attempt_id=attempt, cwd=tmp,
                capability_mode="debug", liveness="working", harness="codex",
                parent_sid="sid-main", is_child=True,
            )
            session = Session(
                harness="codex", pid=900, proc_start="main-start", cwd=tmp,
                slug="main", session_id="sid-main", liveness="working",
            )
            children = [
                DispatchJob(
                    key="code-plan", slug="source-open", depth=2,
                    parent_slug="owner", route_id=r0["route_id"],
                    route_hash=r0["route_hash"], route_file="r0.json",
                    route_node="plan", liveness="working", cwd=tmp,
                    harness="codex",
                ),
                DispatchJob(
                    key="code-test", slug="middle-done", depth=2,
                    parent_slug="owner", route_id=r1["route_id"],
                    route_hash=r1["route_hash"], route_file="r1.json",
                    route_node="test", liveness="idle", status="done", cwd=tmp,
                    harness="codex",
                ),
                DispatchJob(
                    key="code-execute", slug="current", depth=2,
                    parent_slug="owner", route_id=r2["route_id"],
                    route_hash=r2["route_hash"], route_file="r2.json",
                    route_node="execute", liveness="working", cwd=tmp,
                    harness="codex",
                ),
            ]
            projection.attach_projections(
                [session], [owner, *children],
                route_records={row["route_id"]: row for row in (r0, r1, r2)},
                now=100.0,
            )
            self.assertEqual(owner.work_projection.route_id, r2["route_id"])
            self.assertEqual(owner.work_projection.stage_label, "execute")
            self.assertIsNone(owner.work_projection.ambiguity)
            payload = projection.route_summary_from_projections([owner, *children])
            owner_json = next(item for item in payload if item["route_id"] == r2["route_id"])
            self.assertEqual(owner_json["route_id"], r2["route_id"])
            self.assertEqual(children[-1].work_projection.route_id, r2["route_id"])
            render.set_process_view(False)
            group_text = "\n".join(
                "".join(token for token, _kind in line)
                for line in render._build_lines(
                    [session], [owner, *children], section="both", narrow=False,
                    malformed=0, layout="wide", term_width=168,
                ) if line
            )
            render.set_process_view(True)
            process_text = "\n".join(
                "".join(token for token, _kind in line)
                for line in render._build_lines(
                    [session], [owner, *children], section="both", narrow=False,
                    malformed=0, layout="wide", term_width=168,
                ) if line
            )
            render.set_process_view(False)
            self.assertIn("execute", group_text)
            self.assertIn("execute", process_text)

    def test_childless_abandoned_compile_does_not_pollute_linear_lineage(self):
        with tempfile.TemporaryDirectory() as tmp:
            attempt = "att-abandoned"
            r0 = self._lineage_record(tmp, attempt, 0, first_node="plan")
            r1 = self._lineage_record(tmp, attempt, 1, source=r0, first_node="execute")
            abandoned = self._lineage_record(
                tmp, attempt, 0, first_node="review", family="family-abandoned"
            )
            owner = DispatchJob(
                key="autopilot-code", slug="owner", depth=1,
                worker_type="owner", attempt_id=attempt, cwd=tmp,
                capability_mode="debug", liveness="working",
            )
            source = DispatchJob(
                key="code-plan", slug="source", depth=2, parent_slug="owner",
                route_id=r0["route_id"], route_hash=r0["route_hash"],
                route_file="r0.json", route_node="plan", liveness="idle", status="done",
            )
            current = DispatchJob(
                key="code-execute", slug="current", depth=2, parent_slug="owner",
                route_id=r1["route_id"], route_hash=r1["route_hash"],
                route_file="r1.json", route_node="execute", liveness="working",
            )
            projection.attach_projections(
                [], [owner, source, current],
                route_records={
                    r0["route_id"]: r0, r1["route_id"]: r1,
                    abandoned["route_id"]: abandoned,
                }, now=100.0,
            )
            self.assertEqual(owner.work_projection.route_id, r1["route_id"])
            self.assertEqual(owner.work_projection.stage_label, "execute")

    def test_competing_successors_and_unrelated_owner_stay_ambiguous(self):
        with tempfile.TemporaryDirectory() as tmp:
            attempt = "att-competing"
            r0 = self._lineage_record(tmp, attempt, 0, first_node="plan")
            r1a = self._lineage_record(tmp, attempt, 1, source=r0, first_node="execute")
            r1b = self._lineage_record(
                tmp, attempt, 1, source=r0, first_node="test", family="family-linear"
            )
            owner = DispatchJob(
                key="autopilot-code", slug="owner", depth=1,
                worker_type="owner", attempt_id=attempt, cwd=tmp,
                capability_mode="debug", liveness="working",
            )
            jobs = []
            for index, record in enumerate((r0, r1a, r1b)):
                jobs.append(DispatchJob(
                    key="code", slug=f"child-{index}", depth=2, parent_slug="owner",
                    route_id=record["route_id"], route_hash=record["route_hash"],
                    route_file=f"r{index}.json", route_node=record["nodes"][0]["id"],
                    liveness="working",
                ))
            projection.attach_projections(
                [], [owner, *jobs],
                route_records={row["route_id"]: row for row in (r0, r1a, r1b)},
                now=100.0,
            )
            self.assertEqual(owner.work_projection.ambiguity, "multiple-owner-routes")

            unrelated = dict(r1a)
            unrelated["owner_attempt_id"] = "att-unrelated"
            unrelated["route_hash"] = route.route_hash(unrelated)
            unrelated["route_id"] = "rt-" + unrelated["route_hash"].split(":", 1)[1][:16]
            owner2 = DispatchJob(
                key="autopilot-code", slug="owner2", depth=1,
                worker_type="owner", attempt_id=attempt, cwd=tmp,
                capability_mode="debug", liveness="working",
            )
            bad_child = DispatchJob(
                key="code", slug="bad", depth=2, parent_slug="owner2",
                route_id=unrelated["route_id"], route_hash=unrelated["route_hash"],
                route_file="bad.json", route_node="execute", liveness="working",
            )
            projection.attach_projections(
                [], [owner2, bad_child],
                route_records={unrelated["route_id"]: unrelated}, now=100.0,
            )
            self.assertEqual(owner2.work_projection.ambiguity, "multiple-owner-routes")

    def test_resealed_but_tampered_lineage_history_stays_ambiguous(self):
        with tempfile.TemporaryDirectory() as tmp:
            attempt = "att-tampered-lineage"
            r0 = self._lineage_record(tmp, attempt, 0, first_node="plan")
            r1 = self._lineage_record(tmp, attempt, 1, source=r0, first_node="test")
            r2 = self._lineage_record(tmp, attempt, 2, source=r1, first_node="execute")
            # A self-consistent route hash is not proof that the inherited
            # lineage was preserved. Drop R0->R1 and reseal the object.
            r2["supersession_edges"] = [r2["source_route_supersession"]]
            r2["route_hash"] = route.route_hash(r2)
            r2["route_id"] = "rt-" + r2["route_hash"].split(":", 1)[1][:16]
            owner = DispatchJob(
                key="autopilot-code", slug="owner", depth=1,
                worker_type="owner", attempt_id=attempt, cwd=tmp,
                capability_mode="debug", liveness="working",
            )
            children = [
                DispatchJob(
                    key="code", slug=f"child-{index}", depth=2,
                    parent_slug="owner", route_id=record["route_id"],
                    route_hash=record["route_hash"], route_file=f"r{index}.json",
                    route_node=record["nodes"][0]["id"], liveness="working",
                )
                for index, record in enumerate((r0, r1, r2))
            ]
            projection.attach_projections(
                [], [owner, *children],
                route_records={record["route_id"]: record for record in (r0, r1, r2)},
                now=100.0,
            )
            self.assertEqual(owner.work_projection.ambiguity, "multiple-owner-routes")

    def test_same_process_identity_with_two_leaf_routes_fails_closed(self):
        first = DispatchJob(key="code", slug="a", pid=55, proc_start="start",
                            route_id="route-a", route_node="execute")
        second = DispatchJob(key="code", slug="b", pid=55, proc_start="start",
                             route_id="route-b", route_node="test")
        projection.attach_projections([], [first, second], now=100.0)
        self.assertEqual(first.work_projection.ambiguity, "multiple-leaf-candidates")

    def test_unique_exact_and_unique_cwd_candidates_are_adopted(self):
        rid = route.load(REAL)["route_id"]
        leaf = DispatchJob(key="code", slug="leaf", pid=71, proc_start="new",
                           cwd="/route", harness="claude", route_id=rid,
                           route_file=REAL, route_node="execute", liveness="working")
        exact = Session(harness="claude", pid=71, proc_start="new", cwd="/other",
                        session_id="sid-exact", liveness="working")
        cwd = Session(harness="claude", pid=72, proc_start=None, cwd="/route",
                      session_id="sid-cwd", liveness="working")
        projection.attach_projections([exact, cwd], [leaf], now=100.0)
        self.assertEqual((exact.work_projection.source, exact.work_projection.route_node),
                         ("route-exact", "execute"))
        self.assertEqual((cwd.work_projection.source, cwd.work_projection.route_node),
                         ("route-exact", "execute"))

    def test_pid_reuse_and_duplicate_cwd_candidates_refuse_adoption(self):
        rid = route.load(REAL)["route_id"]
        reused = DispatchJob(key="code", slug="reused", pid=71, proc_start="new",
                             cwd="/same", harness="claude", route_id=rid,
                             route_file=REAL, route_node="execute", liveness="working")
        stale_identity = Session(harness="claude", pid=71, proc_start="old", cwd="/same",
                                 session_id="sid-reused", liveness="working")
        projection.attach_projections([stale_identity], [reused], now=100.0)
        self.assertNotEqual(stale_identity.work_projection.source, "route-exact")
        self.assertIsNone(stale_identity.work_projection.route_id)

        first = DispatchJob(key="code", slug="cwd-a", cwd="/same", harness="claude",
                            route_id=rid, route_file=REAL, route_node="plan", liveness="working")
        second = DispatchJob(key="code", slug="cwd-b", cwd="/same", harness="claude",
                             route_id=rid, route_file=REAL, route_node="execute", liveness="working")
        ambiguous = Session(harness="claude", pid=73, cwd="/same", session_id="sid-cwd",
                            liveness="working")
        projection.attach_projections([ambiguous], [first, second], now=100.0)
        self.assertEqual(ambiguous.work_projection.ambiguity,
                         "multiple-child-cwd-candidates")

    def test_attempt_only_and_both_owner_link_contracts_traverse_children(self):
        rid = route.load(REAL)["route_id"]
        session_owner = Session(harness="claude", pid=80, cwd="/owner", session_id="sid-owner",
                                liveness="working")
        session_child = DispatchJob(key="code", slug="sid-child", parent_sid="sid-owner",
                                    route_id=rid, route_file=REAL, route_node="execute",
                                    liveness="working")
        dispatch_owner = DispatchJob(key="code", slug="slug-owner", attempt_id="att-only",
                                     depth=1, liveness="working")
        dispatch_child = DispatchJob(key="code", slug="slug-child", parent_slug="slug-owner",
                                     depth=2, route_id=rid, route_file=REAL, route_node="execute",
                                     liveness="working")
        projection.attach_projections([session_owner],
                                      [session_child, dispatch_owner, dispatch_child], now=100.0)
        self.assertEqual(session_owner.work_projection.source, "route-exact")
        self.assertEqual(dispatch_owner.work_projection.source, "route-exact")
        self.assertEqual(dispatch_owner.work_projection.stage_label, "execute")

    def test_direct_owner_route_conflict_is_fail_closed(self):
        owner_record = route.load(REAL)
        child_record = route.load(COMPOSED)
        owner = DispatchJob(key="owner", slug="owner", depth=1,
                            route_id=owner_record["route_id"], route_file=REAL,
                            route_node="execute", liveness="working")
        child = DispatchJob(key="claim", slug="child", parent_slug="owner", depth=2,
                            route_id=child_record["route_id"], route_file=COMPOSED,
                            route_node="claim-a", liveness="working")
        projection.attach_projections([], [owner, child], now=100.0)
        self.assertEqual(owner.work_projection.ambiguity, "owner-route-conflict")
        self.assertIsNone(owner.work_projection.route_id)

    def test_registered_owner_binding_projects_route_before_first_child(self):
        record = route.load(REAL)
        rid, route_hash = record["route_id"], record["route_hash"]
        with tempfile.TemporaryDirectory() as tmp:
            jobs_path = os.path.join(tmp, "jobs.log")
            metadata = ",".join((
                "capability=autopilot-code", "capability_mode=debug",
                "attempt_schema_version=2", "dispatch_depth=1",
                "transport=headless", "execution_surface=registered-headless",
                "registered_worker=1", "fallback_hop=same-harness-headless",
                "worker_type=owner", "assigned_contract=autopilot-code",
                "owner=autopilot-code", "intensity=standard",
                "attempt_id=att-owner-binding",
                "owner_route_file=" + REAL, "owner_route_id=" + rid,
                "owner_route_hash=" + route_hash,
            ))
            with open(jobs_path, "w", encoding="utf-8") as stream:
                stream.write("2099-01-01T00:00:00Z\topen\t/repo\t/worktree\towner\t"
                             + metadata + "\n")
            jobs, malformed = dispatch._scan_jobs_log(jobs_path, set())

        self.assertEqual(malformed, 0)
        self.assertEqual((jobs[0].owner_route_id, jobs[0].owner_route_hash),
                         (rid, route_hash))
        projection.attach_projections([], jobs, now=100.0)
        owner = jobs[0]
        self.assertEqual(owner.work_projection.source, "route-exact")
        self.assertEqual(owner.work_projection.route_id, rid)
        self.assertEqual(owner.work_projection.progress.total, 4)
        # No child has attached yet, so no node is sealed active — the scalar
        # selection stays unknown rather than guessing the first node.
        self.assertIsNone(owner.work_projection.route_node)
        self.assertEqual(owner.work_projection.node_state, "unknown")
        lines = render._build_lines([], jobs, section="dispatch", narrow=False,
                                    malformed=0, layout="wide")
        text = "\n".join("".join(token for token, _kind in line)
                         for line in lines if line)
        for stage in ("plan", "execute", "test", "report"):
            self.assertIn(stage, text)
        self.assertNotIn("preparing…", text)
        self.assertNotIn("running", text)

    def test_owner_binding_adopts_started_child_and_rejects_conflicts(self):
        owner_record = route.load(REAL)
        rid, route_hash = owner_record["route_id"], owner_record["route_hash"]
        owner = DispatchJob(
            key="code", slug="owner", depth=1, worker_type="owner",
            attempt_id="att-owner", liveness="working",
            owner_route_file=REAL, owner_route_id=rid, owner_route_hash=route_hash,
        )
        child = DispatchJob(
            key="code-execute", slug="child", depth=2, parent_slug="owner",
            route_id=rid, route_file=REAL, route_hash=route_hash,
            route_node="execute", assigned_contract="code-execute",
            liveness="working",
        )
        projection.attach_projections([], [owner, child], now=100.0)
        self.assertEqual(owner.work_projection.stage_label, "execute")
        self.assertEqual(owner.work_projection.route_node, "execute")
        self.assertEqual(owner.work_projection.node_state, "active")
        with mock.patch.object(render, "_BLINK_ON", True):
            lines = render._build_lines([], [owner, child], section="dispatch",
                                        narrow=False, malformed=0, layout="wide")
        text = "\n".join("".join(token for token, _kind in line)
                         for line in lines if line)
        self.assertIn("running", text)

        other = route.load(COMPOSED)
        conflict = DispatchJob(
            key="claim", slug="other", depth=2, parent_slug="owner",
            route_id=other["route_id"], route_file=COMPOSED,
            route_hash=other["route_hash"], route_node="claim-a",
            liveness="working",
        )
        # F-88: mixed generations — a live child still on the sealed route plus
        # a child on another route is genuinely ambiguous and stays fail-closed.
        projection.attach_projections([], [owner, child, conflict], now=100.0)
        self.assertEqual(owner.work_projection.ambiguity, "owner-route-conflict")
        # F-88: when EVERY route-carrying child has moved to one verified
        # successor route (the owner re-compiled mid-attempt), the owner adopts
        # the successor instead of blanking its stage projection.
        projection.attach_projections([], [owner, conflict], now=100.0)
        self.assertEqual(owner.work_projection.source, "route-exact")
        self.assertEqual(owner.work_projection.route_id, other["route_id"])
        self.assertEqual(owner.work_projection.route_node, "claim-a")
        self.assertIsNone(owner.work_projection.ambiguity)

    def test_owner_binding_keeps_verified_successor_during_child_gap(self):
        owner_record = route.load(REAL)
        successor = route.load(COMPOSED)
        owner = DispatchJob(
            key="code", slug="owner", depth=1, worker_type="owner",
            attempt_id="att-owner-gap", liveness="working",
            owner_route_file=REAL, owner_route_id=owner_record["route_id"],
            owner_route_hash=owner_record["route_hash"],
        )
        owner._registry_path = "/state/dispatch/jobs.log"
        evidence = {
            owner_record["route_id"]: {
                "plan": {
                    "parent": "owner", "parent_attempt_id": "att-owner-gap",
                    "registry_order": 10,
                    "_registry_path": "/state/dispatch/jobs.log",
                    "route_file": REAL, "route_hash": owner_record["route_hash"],
                    "status": "done", "note": "dead-worker-fail",
                },
            },
            successor["route_id"]: {
                "claim-a": {
                    "parent": "owner", "parent_attempt_id": "att-owner-gap",
                    "registry_order": 20,
                    "_registry_path": "/state/dispatch/jobs.log",
                    "route_file": COMPOSED, "route_hash": successor["route_hash"],
                    "status": "done", "note": "completed-marker",
                },
            },
        }

        # No live child is present: this is the exact gap between two successor
        # attempts that previously snapped the owner back to its launch route.
        projection.attach_projections(
            [], [owner], route_records={
                owner_record["route_id"]: owner_record,
                successor["route_id"]: successor,
            }, node_evidence=evidence, now=100.0,
        )
        self.assertEqual(owner.work_projection.source, "route-exact")
        self.assertEqual(owner.work_projection.route_id, successor["route_id"])
        self.assertIsNone(owner.work_projection.ambiguity)
        self.assertEqual(
            [node_id for node_id, _state in render._projection_route_seq(owner)],
            ["survey", "claim-a", "claim-b", "synth"],
        )

        # A newer row belonging to another owner generation cannot carry this
        # owner's successor across the gap; exact attempt ownership wins over
        # the reused display slug.
        evidence[successor["route_id"]]["claim-a"]["parent_attempt_id"] = "att-other"
        projection.attach_projections(
            [], [owner], route_records={
                owner_record["route_id"]: owner_record,
                successor["route_id"]: successor,
            }, node_evidence=evidence, now=100.0,
        )
        self.assertEqual(owner.work_projection.route_id, owner_record["route_id"])

    def test_owner_gap_fails_closed_for_unverified_latest_successor(self):
        owner_record = route.load(REAL)
        successor = route.load(COMPOSED)
        owner = DispatchJob(
            key="code", slug="owner", depth=1, worker_type="owner",
            attempt_id="att-owner-gap", liveness="working",
            owner_route_file=REAL, owner_route_id=owner_record["route_id"],
            owner_route_hash=owner_record["route_hash"],
        )
        owner._registry_path = "/state/dispatch/jobs.log"
        evidence = {successor["route_id"]: {"claim-a": {
            "parent": "owner", "parent_attempt_id": "att-owner-gap",
            "registry_order": 20, "_registry_path": "/state/dispatch/jobs.log",
            "route_file": COMPOSED, "route_hash": "sha256:not-the-route",
            "status": "done",
        }}}
        projection.attach_projections(
            [], [owner], route_records={
                owner_record["route_id"]: owner_record,
                successor["route_id"]: successor,
            }, node_evidence=evidence, now=100.0,
        )
        self.assertEqual(owner.work_projection.ambiguity, "owner-route-conflict")
        self.assertIsNone(owner.work_projection.route_id)

    def _fake_owner_route_binding_module(self, current, status):
        class _OwnerRouteBinding:
            def __init__(self, route_file, route_id, route_hash):
                self.route_file, self.route_id, self.route_hash = route_file, route_id, route_hash

        class _OwnerRouteBindingError(ValueError):
            pass

        module = mock.Mock()
        module.OwnerRouteBinding = _OwnerRouteBinding
        module.OwnerRouteBindingError = _OwnerRouteBindingError
        module.resolve_owner_route_lifecycle = mock.Mock(return_value=(current, status))
        return module

    def test_verified_advance_record_collapses_mixed_generation_children(self):
        # R0->R1->R2 verified lineage: a live child stranded on R0 (mixed
        # generations, which F-88 alone would flag as owner-route-conflict)
        # no longer matters once a durable advance record proves the owner
        # moved on -- the successor route projects outright.
        r0 = route.load(REAL)
        r2 = route.load(COMPOSED)
        owner = DispatchJob(
            key="code", slug="owner", depth=1, worker_type="owner",
            attempt_id="att-owner-advance", liveness="working",
            owner_route_file=REAL, owner_route_id=r0["route_id"],
            owner_route_hash=r0["route_hash"],
        )
        owner._registry_path = "/state/dispatch/jobs.log"
        stale_child = DispatchJob(
            key="plan", slug="stale-child", depth=2, parent_slug="owner",
            route_id=r0["route_id"], route_file=REAL, route_hash=r0["route_hash"],
            route_node="plan", liveness="working",
        )
        current_binding = mock.Mock(
            route_file=COMPOSED, route_id=r2["route_id"], route_hash=r2["route_hash"],
        )
        fake_binding_module = self._fake_owner_route_binding_module(
            current_binding, "owner-route-advance-current",
        )
        with mock.patch.object(
            projection, "_owner_route_binding_module", return_value=fake_binding_module,
        ):
            projection.attach_projections(
                [], [owner, stale_child], route_records={
                    r0["route_id"]: r0, r2["route_id"]: r2,
                }, now=100.0,
            )
        self.assertEqual(owner.work_projection.source, "route-exact")
        self.assertEqual(owner.work_projection.route_id, r2["route_id"])
        self.assertIsNone(owner.work_projection.ambiguity)

    def test_conflicting_advance_evidence_is_a_typed_ambiguity(self):
        r0 = route.load(REAL)
        owner = DispatchJob(
            key="code", slug="owner", depth=1, worker_type="owner",
            attempt_id="att-owner-tampered", liveness="working",
            owner_route_file=REAL, owner_route_id=r0["route_id"],
            owner_route_hash=r0["route_hash"],
        )
        owner._registry_path = "/state/dispatch/jobs.log"
        fake_binding_module = self._fake_owner_route_binding_module(None, "unused")
        fake_binding_module.resolve_owner_route_lifecycle = mock.Mock(
            side_effect=fake_binding_module.OwnerRouteBindingError("owner-route-advance-target-invalid"),
        )
        with mock.patch.object(
            projection, "_owner_route_binding_module", return_value=fake_binding_module,
        ):
            projection.attach_projections(
                [], [owner], route_records={r0["route_id"]: r0}, now=100.0,
            )
        self.assertEqual(owner.work_projection.ambiguity, "owner-route-advance-conflict")

    def test_two_child_adopted_durable_successors_use_multiple_owner_routes(self):
        r0 = route.load(REAL)
        owner = DispatchJob(
            key="code", slug="owner", depth=1, worker_type="owner",
            attempt_id="att-owner-competing", liveness="working",
            owner_route_file=REAL, owner_route_id=r0["route_id"],
            owner_route_hash=r0["route_hash"],
        )
        owner._registry_path = "/state/dispatch/jobs.log"
        fake = self._fake_owner_route_binding_module(None, "unused")
        fake.resolve_owner_route_lifecycle = mock.Mock(
            side_effect=fake.OwnerRouteBindingError(
                "owner-route-advance-competing-successor"
            ),
        )
        with mock.patch.object(
            projection, "_owner_route_binding_module", return_value=fake,
        ):
            projection.attach_projections(
                [], [owner], route_records={r0["route_id"]: r0}, now=100.0,
            )
        self.assertEqual(owner.work_projection.ambiguity, "multiple-owner-routes")

    def test_owner_projection_collapses_parallel_active_nodes(self):
        record = {
            "schema_version": 1,
            "nodes": [
                {"id": "frame", "depends_on": [], "parallel_group": "frame"},
                {"id": "frame-alt", "depends_on": [], "parallel_group": "frame"},
                {"id": "next", "depends_on": ["frame", "frame-alt"]},
            ],
        }
        record["route_hash"] = route.route_hash(record)
        digest = record["route_hash"].split(":", 1)[1]
        record["route_id"] = "rt-" + digest[:16]
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "parallel.route.json")
            with open(path, "w", encoding="utf-8") as stream:
                json.dump(record, stream)
            rid, route_hash_ = record["route_id"], record["route_hash"]
            owner = DispatchJob(
                key="code", slug="owner", depth=1, worker_type="owner",
                attempt_id="att-owner", liveness="working",
                owner_route_file=path, owner_route_id=rid, owner_route_hash=route_hash_,
            )
            leg_a = DispatchJob(
                key="frame", slug="leg-a", depth=2, parent_slug="owner",
                route_id=rid, route_file=path, route_hash=route_hash_,
                route_node="frame", liveness="working",
            )
            leg_b = DispatchJob(
                key="frame", slug="leg-b", depth=2, parent_slug="owner",
                route_id=rid, route_file=path, route_hash=route_hash_,
                route_node="frame-alt", liveness="working",
            )
            projection.attach_projections([], [owner, leg_a, leg_b], now=100.0)
        self.assertEqual(owner.work_projection.route_node, "frame(2-way)")
        self.assertEqual(owner.work_projection.node_state, "active")

    def test_owner_render_uses_node_states_not_progress(self):
        owner_record = route.load(REAL)
        rid, route_hash = owner_record["route_id"], owner_record["route_hash"]
        owner = DispatchJob(
            key="code", slug="owner", depth=1, worker_type="owner",
            attempt_id="att-owner", liveness="working",
            owner_route_file=REAL, owner_route_id=rid, owner_route_hash=route_hash,
        )
        child = DispatchJob(
            key="code-execute", slug="child", depth=2, parent_slug="owner",
            route_id=rid, route_file=REAL, route_hash=route_hash,
            route_node="execute", assigned_contract="code-execute",
            liveness="working",
        )
        projection.attach_projections([], [owner, child], now=100.0)
        # Only `execute` is actually active; `plan` has no completed evidence and
        # must stay pending rather than being inferred done from its position
        # ahead of the active node.
        view = owner.work_projection._route_view["view"]
        states = {n["id"]: n["state"] for n in view["nodes"]}
        self.assertEqual(states, {"plan": "pending", "execute": "active",
                                  "test": "pending", "report": "pending"})
        route_seq = [(n["id"], n["state"]) for n in view["nodes"]]
        self.assertEqual(render._route_current_index(route_seq), 1)
        self.assertEqual(render._depth1_rail_color_index("code", None, route_seq), 1)
        with mock.patch.object(render, "_BLINK_ON", True):
            lines = render._build_lines([], [owner, child], section="dispatch",
                                        narrow=False, malformed=0, layout="wide")
        text = "\n".join("".join(token for token, _kind in line)
                         for line in lines if line)
        self.assertNotIn("plan ✓", text)
        self.assertNotIn("test ✓", text)
        self.assertNotIn("report ✓", text)

    def test_partial_owner_binding_fails_closed(self):
        record = route.load(REAL)
        owner = DispatchJob(
            key="code", slug="owner", depth=1, worker_type="owner",
            owner_route_file=REAL, owner_route_id=record["route_id"],
            liveness="working",
        )
        projection.attach_projections([], [owner], now=100.0)
        self.assertEqual(owner.work_projection.source, "registry-exact")
        self.assertEqual(owner.work_projection.ambiguity,
                         "owner-route-binding-invalid")

    def test_old_route_keys_and_private_evidence_remain_compatible(self):
        rid = route.load(REAL)["route_id"]
        job = DispatchJob(key="code", slug="old-consumer", route_id=rid, route_file=REAL,
                          route_node="execute", model="opus", harness="claude", effort="high",
                          liveness="working")
        projection.attach_projections([], [job], now=100.0)
        payload = projection.route_summary_from_projections([job])[0]
        node = next(item for item in payload["nodes"] if item["id"] == "execute")
        self.assertEqual(payload["source"], "record")
        self.assertEqual({key: node[key] for key in ("model", "harness", "effort", "elapsed_min", "note")},
                         {"model": "opus", "harness": "claude", "effort": "high",
                          "elapsed_min": None, "note": None})
        self.assertIsInstance(job.to_dict()["work_projection"]["ambiguity"], list)
        self.assertNotIn("_context_evidence", json.dumps(job.to_dict()))

    def test_qa_artifact_lookup_is_exact_and_separate_from_stage(self):
        from fleet.collectors import dispatch
        with tempfile.TemporaryDirectory() as tmp:
            plan = os.path.join(tmp, ".agent_reports", "plans", "2026-07-22_qa-job")
            os.makedirs(os.path.join(plan, "plan"))
            with open(os.path.join(plan, "plan", "plan.md"), "w", encoding="utf-8") as stream:
                stream.write("qa_level: standard\n")
            job = DispatchJob(key="code", slug="qa-job", cwd=tmp)
            self.assertEqual(dispatch.resolve_plan_qa_artifact(job), "standard")


if __name__ == "__main__":
    unittest.main()
