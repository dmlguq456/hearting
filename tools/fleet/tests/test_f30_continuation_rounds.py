"""Focused semantic-round and continuation-lineage checks (F-91)."""
import json
import hashlib
import os
import shutil
import sys
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from fleet import projection, render, route  # noqa: E402
from fleet.collectors import dispatch  # noqa: E402
from fleet.model import DispatchJob, Session  # noqa: E402


FIXTURE_DIR = os.path.join(os.path.dirname(__file__), "fixtures", "route", "continuation_round")


class ContinuationRoundTest(unittest.TestCase):
    def setUp(self):
        route.clear_cache()
        self.gen0 = route.load(os.path.join(FIXTURE_DIR, "gen0.json"))
        self.gen1 = route.load(os.path.join(FIXTURE_DIR, "gen1.json"))
        self.records = {self.gen0["route_id"]: self.gen0, self.gen1["route_id"]: self.gen1}

    def _evidence(self, current=True):
        def item(attempt, status, pid=None, note=None, parent="att-fixture-owner"):
            return {"attempt_id": attempt, "status": status, "pid": pid,
                    "contract_status": "current", "parent_attempt_id": parent,
                    "note": note}

        evidence = {
            self.gen0["route_id"]: {
                "plan": {"status": "done", "attempt_history": [
                    item("att-fixture-plan-0", "done", 101, "completed-marker")
                ]},
                "plan-alternative": {"status": "done", "attempt_history": [
                    item("att-fixture-plan-alt-0", "done", 102, "completed-marker")
                ]},
                "plan-check": {"status": "done", "attempt_history": [
                    item("att-fixture-plan-check-0", "done", 103, "completed-review")
                ]},
                "execute": {"status": "done", "attempt_history": [
                    item("att-fixture-execute-0", "done", 104, "completed-marker")
                ]},
            },
            self.gen1["route_id"]: {
                "plan": {"status": "running", "attempt_history": [
                    item("att-fixture-plan-1", "running", 201)
                ]},
                "plan-alternative": {"status": "running", "attempt_history": [
                    item("att-fixture-plan-alt-1", "running", 202)
                ]},
                "plan-check": {"status": "open", "attempt_history": [
                    item("att-fixture-plan-check-1", "open")
                ]},
                "execute": {"status": "open", "attempt_history": [
                    item("att-fixture-execute-1", "open")
                ]},
            },
        }
        if not current:
            evidence[self.gen1["route_id"]]["plan"]["attempt_history"] = []
        return evidence

    def _jobs(self):
        return [DispatchJob(
            key="plan", slug="lineage-plan", route_id=self.gen1["route_id"],
            route_file=os.path.join(FIXTURE_DIR, "gen1.json"), route_hash=self.gen1["route_hash"],
            route_node="plan", liveness="working", parent_slug="fixture", pid=201,
            proc_start="fixture-201", harness="codex", depth=2,
        )]

    def _job(self, record, node="plan", **kwargs):
        return DispatchJob(
            key=node, slug="lineage-" + node, route_id=record["route_id"],
            route_file=os.path.join(FIXTURE_DIR, "gen1.json"),
            route_hash=record["route_hash"], route_node=node,
            liveness="working", parent_slug="fixture", pid=201, proc_start="fixture-201",
            harness="codex", depth=2, **kwargs,
        )

    @staticmethod
    def _reseal(record):
        digest = route.route_hash(record)
        record["route_hash"] = digest
        record["route_id"] = "rt-" + digest.split(":", 1)[1][:16]
        return record

    def _paired_records(self, mutate_predecessor=None, mutate_successor=None):
        predecessor = deepcopy(self.gen0)
        successor = deepcopy(self.gen1)
        if mutate_predecessor:
            mutate_predecessor(predecessor)
        self._reseal(predecessor)
        edge = dict(successor["source_route_supersession"])
        edge.update({"from_route_id": predecessor["route_id"],
                     "from_route_hash": predecessor["route_hash"]})
        successor.update({"source_route_id": predecessor["route_id"],
                          "source_route_hash": predecessor["route_hash"],
                          "source_route_supersession": edge,
                          "supersession_edges": [*(predecessor.get("supersession_edges") or []), edge]})
        if mutate_successor:
            mutate_successor(successor)
        self._reseal(successor)
        return predecessor, successor

    def _evidence_for(self, records, base=None):
        source = deepcopy(self._evidence() if base is None else base)
        result = {}
        for record in records.values():
            rid = record["route_id"]
            if rid in source:
                result[rid] = deepcopy(source[rid])
                continue
            generation = record.get("advance_generation")
            original_id = {
                0: self.gen0["route_id"],
                1: self.gen1["route_id"],
            }.get(generation)
            if original_id is None and record.get("source_route_id"):
                original_id = self.gen1["route_id"]
            if original_id in source:
                result[rid] = deepcopy(source[original_id])
        return result

    def _view(self, records, successor, evidence=None, jobs=None):
        records = {record["route_id"]: record for record in records.values()}
        evidence = self._evidence_for(records, evidence)
        jobs = list(jobs or [self._job(successor)])
        view = next(view for view in route.build_views(jobs, evidence, records, 100.0)
                    if view["route_id"] == successor["route_id"])
        return view, evidence, records

    def _assert_conservative_surface(self, records, successor, expected_reason, evidence=None):
        """An unverified lineage must stay route-local on every public surface."""
        records = {record["route_id"]: record for record in records.values()}
        evidence = self._evidence_for(records, evidence)
        jobs = [self._job(successor)]
        view = next(item for item in route.build_views(jobs, evidence, records, 100.0)
                    if item["route_id"] == successor["route_id"])
        plan = next(node for node in view["nodes"] if node["id"] == "plan")
        self.assertEqual(view["lineage"]["reason"], expected_reason)
        self.assertEqual((plan["attempt_round"], plan["route_attempt_round"]), (1, 1))
        self.assertIsNone(plan["prior_attempt_rounds"])
        self.assertIsNone(plan["revision_of"])

        card = render._route_card_l2(view, 120)
        card_text = "\n".join("".join(token for token, _kind in row) for row in card)
        self.assertIn("plan(R1)", card_text)
        self.assertNotIn("→", card_text)

        direct = route.summary([view])[0]
        direct_plan = next(node for node in direct["nodes"] if node["id"] == "plan")
        self.assertEqual(direct["lineage"]["reason"], expected_reason)
        self.assertEqual(direct_plan["attempt_round"], 1)
        self.assertIsNone(direct_plan.get("revision_of"))

        projection.attach_projections([], jobs, route_records=records,
                                      node_evidence=evidence, now=100.0,
                                      spec_markers={}, capability_groundings={})
        live = next(item for item in projection.route_summary_from_projections(jobs)
                    if item["route_id"] == successor["route_id"])
        live_plan = next(node for node in live["nodes"] if node["id"] == "plan")
        self.assertEqual(live["lineage"]["reason"], expected_reason)
        self.assertEqual(live_plan["attempt_round"], 1)
        self.assertIsNone(live_plan.get("revision_of"))

    def test_t1_baseline_and_t3_single_route_retries_are_route_local(self):
        evidence = {self.gen0["route_id"]: self._evidence()[self.gen0["route_id"]]}
        jobs = [self._job(self.gen0)]
        views = route.build_views(jobs, evidence, {self.gen0["route_id"]: self.gen0}, 100.0)
        view = views[0]
        plan = next(node for node in view["nodes"] if node["id"] == "plan")
        self.assertEqual((plan["attempt_round"], plan["route_attempt_round"]), (1, 1))
        self.assertNotIn("lineage", view)
        self.assertIsNone(plan["revision_of"])
        self.assertNotIn("lineage", route.summary(views)[0])

        retry = deepcopy(evidence)
        retry[self.gen0["route_id"]]["plan"]["attempt_history"].extend([
            {"attempt_id": "att-single-retry", "status": "done", "pid": 301,
             "contract_status": "current"},
            {"attempt_id": "att-single-retry", "status": "done", "pid": 301,
             "contract_status": "current"},
        ])
        retry_view = route.build_views(jobs, retry, {self.gen0["route_id"]: self.gen0}, 100.0)[0]
        retry_plan = next(node for node in retry_view["nodes"] if node["id"] == "plan")
        self.assertEqual(retry_plan["attempt_round"], 2)
        self.assertEqual(retry_plan["attempt_round"], retry_plan["route_attempt_round"])
        self.assertNotIn("lineage", retry_view)

    def test_t2_group_process_and_live_json_agree_on_verified_round_and_revision(self):
        evidence = self._evidence()
        jobs = self._jobs()
        views = route.build_views(jobs, evidence, self.records, 100.0)
        view = next(view for view in views if view["route_id"] == self.gen1["route_id"])
        labels = [label for label, _state in render._projection_route_seq(
            type("Entity", (), {"work_projection": type("Projection", (), {
                "source": "route-exact", "_route_view": {"view": view}
            })()})()
        )]
        self.assertIn("plan(R2·2-way)", labels)
        card = render._route_card_l2(view, 120)
        card_text = "\n".join("".join(token for token, _kind in row) for row in card)
        self.assertIn("plan R2", card_text)
        self.assertIn("plan-check R1 → plan R2", card_text)
        projection.attach_projections([], jobs, route_records=self.records,
                                      node_evidence=evidence, now=100.0,
                                      spec_markers={}, capability_groundings={})
        live = projection.route_summary_from_projections(jobs)
        live_plan = next(node for node in live[0]["nodes"] if node["id"] == "plan")
        direct_plan = next(node for node in view["nodes"] if node["id"] == "plan")
        self.assertEqual(live_plan["attempt_round"], direct_plan["attempt_round"])
        self.assertEqual(live[0]["lineage"], view["lineage"])

    def test_t4_reuse_t5_generation_two_and_t6_two_way_are_conservative(self):
        predecessor, successor = self._paired_records(
            mutate_successor=lambda record: record.update({
                "reused_nodes": [{"node_id": "plan", "completion_marker": "plan.done"}],
                "source_evidence_digest": "sha256:" + hashlib.sha256(
                    route._canonical([{"node_id": "plan", "completion_marker": "plan.done"}])
                ).hexdigest(),
            })
        )
        evidence = self._evidence_for({predecessor["route_id"]: predecessor,
                                       successor["route_id"]: successor})
        evidence[successor["route_id"]]["plan"]["attempt_history"] = []
        evidence[successor["route_id"]]["plan-check"]["attempt_history"] = [{
            "attempt_id": "att-fixture-plan-check-1", "status": "running", "pid": 205,
            "contract_status": "current"}]
        view, _evidence, records = self._view(
            {predecessor["route_id"]: predecessor, successor["route_id"]: successor}, successor,
            evidence,
        )
        nodes = {node["id"]: node for node in view["nodes"]}
        self.assertEqual(nodes["plan"]["attempt_round"], 1)
        self.assertEqual(nodes["plan-check"]["attempt_round"], 2)
        self.assertIsNone(nodes["plan"]["revision_of"])

        gen2 = deepcopy(successor)
        gen2["advance_generation"] = 2
        gen2["reused_nodes"] = []
        gen2["source_route_id"] = successor["route_id"]
        gen2["source_route_hash"] = successor["route_hash"]
        edge = dict(successor["source_route_supersession"])
        edge.update({"from_route_id": successor["route_id"],
                     "from_route_hash": successor["route_hash"],
                     "to_continuation_id": "cont-fixture-gen2"})
        gen2["continuation_id"] = "cont-fixture-gen2"
        gen2["source_route_supersession"] = edge
        gen2["supersession_edges"] = [*(successor.get("supersession_edges") or []), edge]
        gen2["source_evidence_digest"] = "sha256:" + hashlib.sha256(
            route._canonical([])).hexdigest()
        gen2["owner_attempt_id"] = successor["owner_attempt_id"]
        self._reseal(gen2)
        gen2_evidence = self._evidence_for({predecessor["route_id"]: predecessor,
                                             successor["route_id"]: successor})
        gen2_evidence[gen2["route_id"]] = deepcopy(
            gen2_evidence[successor["route_id"]]
        )
        gen2_evidence[gen2["route_id"]]["plan"]["attempt_history"] = [{
            "attempt_id": "att-fixture-plan-2", "status": "running", "pid": 202,
            "contract_status": "current"}]
        gen2_evidence[successor["route_id"]]["plan"]["attempt_history"] = [{
            "attempt_id": "att-fixture-plan-1", "status": "running", "pid": 201,
            "contract_status": "current"}]
        gen2_evidence[gen2["route_id"]]["plan-check"]["attempt_history"] = [{
            "attempt_id": "att-fixture-plan-check-2", "status": "done", "pid": 203,
            "contract_status": "current"}]
        gen2_records = {r["route_id"]: r for r in (predecessor, successor, gen2)}
        gen2_view = next(view for view in route.build_views(
            [self._job(gen2)], gen2_evidence, gen2_records, 100.0
        ) if view["route_id"] == gen2["route_id"])
        gen2_plan = next(node for node in gen2_view["nodes"] if node["id"] == "plan")
        self.assertEqual(gen2_plan["attempt_round"], 3)
        self.assertEqual(gen2_view["lineage"]["chain"], [predecessor["route_id"], successor["route_id"]])

        duplicate = deepcopy(gen2_evidence)
        duplicate[gen2["route_id"]]["plan-alternative"]["attempt_history"] = [
            {"attempt_id": "att-fixture-plan-alt-2", "status": "running", "pid": 204,
             "contract_status": "current"},
            {"attempt_id": "att-fixture-plan-alt-2", "status": "done", "pid": 204,
             "contract_status": "current"},
        ]
        duplicate[gen2["route_id"]]["plan"]["attempt_history"].append(
            {"attempt_id": "att-fixture-plan-2", "status": "done", "pid": 202,
             "contract_status": "current"})
        dup_view = next(view for view in route.build_views(
            [self._job(gen2)], duplicate, gen2_records, 100.0
        ) if view["route_id"] == gen2["route_id"])
        dup_plan = next(node for node in dup_view["nodes"] if node["id"] == "plan")
        self.assertEqual(dup_plan["attempt_round"], 3)
        collapsed = render._collapse_parallel_nodes(dup_view["nodes"])
        group = next(node for node in collapsed if node["id"].startswith("plan("))
        self.assertEqual((group["id"], group["attempt_round"]), ("plan(2-way)", 3))

    def test_t8_identity_hash_owner_and_malformed_matrix_is_typed_and_fails_closed(self):
        cases = [
            ("missing-hash", lambda record: record.update({"source_route_hash": "sha256:wrong",
                                                            "source_route_supersession": {
                                                                **record["source_route_supersession"],
                                                                "from_route_hash": "sha256:wrong"}}),
             "predecessor-hash-mismatch"),
            ("missing-identity", lambda record: [record.pop(key, None) for key in
                                                   ("capability", "capability_mode", "cwd", "artifact_root")],
             "identity-missing"),
            ("bad-mode", lambda record: record.update({"capability_mode": 7}), "identity-missing"),
            ("mode-mismatch", lambda record: record.update({"capability_mode": "other"}),
             "identity-mismatch"),
            ("bad-edge", lambda record: record["source_route_supersession"].update({"operation": "fork"}),
             "edge-malformed"),
            ("bad-history", lambda record: record.update({"supersession_edges": []}),
             "edge-history-mismatch"),
            ("bad-generation", lambda record: record.update({"advance_generation": 9}),
             "generation-not-monotonic"),
            ("bad-digest", lambda record: record.update({"source_evidence_digest": "sha256:bad"}),
             "evidence-digest-mismatch"),
        ]
        for name, mutate, expected in cases:
            with self.subTest(name=name):
                predecessor, successor = self._paired_records(mutate_successor=mutate)
                records = {predecessor["route_id"]: predecessor, successor["route_id"]: successor}
                result = route.continuation_lineage(successor, records)
                self.assertEqual((result["status"], result["reason"]), ("unverified", expected))
                view, _evidence, _records = self._view(records, successor)
                plan = next(node for node in view["nodes"] if node["id"] == "plan")
                self.assertEqual(plan["attempt_round"], 1)
                self.assertIsNone(plan["revision_of"])

        predecessor, successor = self._paired_records(
            mutate_predecessor=lambda record: [record.pop(key, None) for key in
                                               ("capability", "capability_mode", "cwd", "artifact_root")],
            mutate_successor=lambda record: [record.pop(key, None) for key in
                                             ("capability", "capability_mode", "cwd", "artifact_root")],
        )
        records = {predecessor["route_id"]: predecessor, successor["route_id"]: successor}
        self.assertEqual(route.continuation_lineage(successor, records)["reason"], "identity-missing")
        self._assert_conservative_surface(records, successor, "identity-missing")

        predecessor, successor = self._paired_records(
            mutate_predecessor=lambda record: record.update({"owner_attempt_id": "att-A"}),
            mutate_successor=lambda record: record.update({"owner_attempt_id": "att-B"}),
        )
        records = {predecessor["route_id"]: predecessor, successor["route_id"]: successor}
        self.assertEqual(route.continuation_lineage(successor, records)["reason"], "foreign-owner")
        self._assert_conservative_surface(records, successor, "foreign-owner")
        predecessor, successor = self._paired_records()
        records = {predecessor["route_id"]: predecessor, successor["route_id"]: successor}
        self.assertEqual(route.continuation_lineage(successor, records)["reason"], "owner-unproven")
        unbound = self._evidence()
        for route_evidence in unbound.values():
            for node_evidence in route_evidence.values():
                for item in node_evidence.get("attempt_history") or ():
                    item.pop("parent_attempt_id", None)
        self._assert_conservative_surface(records, successor, "owner-unproven", unbound)

    def test_t8a_t8f_t8g_unavailable_branch_cycle_and_hop_limits(self):
        missing = route.continuation_lineage(self.gen1, {self.gen1["route_id"]: self.gen1})
        self.assertEqual(missing["reason"], "predecessor-record-unavailable")
        self._assert_conservative_surface({self.gen1["route_id"]: self.gen1}, self.gen1,
                                          "predecessor-record-unavailable")

        first, second = self._paired_records()
        branch = deepcopy(second)
        branch["continuation_id"] = "cont-branch"
        branch["source_route_supersession"] = dict(second["source_route_supersession"],
                                                     to_continuation_id="cont-branch")
        branch["supersession_edges"] = [branch["source_route_supersession"]]
        self._reseal(branch)
        result = route.continuation_lineage(
            second, {first["route_id"]: first, second["route_id"]: second,
                     branch["route_id"]: branch})
        self.assertEqual(result["reason"], "lineage-branching")
        self._assert_conservative_surface(
            {first["route_id"]: first, second["route_id"]: second,
             branch["route_id"]: branch}, second, "lineage-branching")

        cycle_a = deepcopy(first)
        cycle_b = deepcopy(second)
        cycle_a.update({"route_id": "rt-cycle-a", "route_hash": "sha256:cycle-a",
                        "source_route_id": "rt-cycle-b", "source_route_hash": "sha256:cycle-b",
                        "advance_generation": 2, "continuation_id": "cont-cycle-a"})
        cycle_b.update({"route_id": "rt-cycle-b", "route_hash": "sha256:cycle-b",
                        "source_route_id": "rt-cycle-a", "source_route_hash": "sha256:cycle-a",
                        "advance_generation": 1, "continuation_id": "cont-cycle-b"})
        for record, source_id, source_hash in ((cycle_a, "rt-cycle-b", "sha256:cycle-b"),
                                                (cycle_b, "rt-cycle-a", "sha256:cycle-a")):
            edge = {"edge_version": 1, "operation": "continuation",
                    "from_route_id": source_id, "from_route_hash": source_hash,
                    "to_continuation_id": record["continuation_id"],
                    "source_verdict_preserved": True}
            record["source_route_supersession"] = edge
            record["supersession_edges"] = [edge]
            record["source_evidence_digest"] = "sha256:" + hashlib.sha256(
                route._canonical(record["reused_nodes"])).hexdigest()
        cycle_a["supersession_edges"] = [cycle_b["source_route_supersession"],
                                          cycle_a["source_route_supersession"]]
        cycle_b["supersession_edges"] = [cycle_b["source_route_supersession"]]
        cycle_records = {"rt-cycle-a": cycle_a, "rt-cycle-b": cycle_b}
        self.assertEqual(route.continuation_lineage(cycle_a, cycle_records)["reason"],
                         "lineage-cycle")
        self._assert_conservative_surface(cycle_records, cycle_a, "lineage-cycle")
        self.assertEqual(route.continuation_lineage(self.gen1, self.records, max_hops=0)["reason"],
                         "lineage-hop-limit")
        original_lineage = route.continuation_lineage
        with mock.patch.object(
            route, "continuation_lineage",
            side_effect=lambda record, records, node_evidence=None, jobs=(), max_hops=0:
                original_lineage(record, records, node_evidence, jobs, max_hops=0),
        ):
            self._assert_conservative_surface(self.records, self.gen1, "lineage-hop-limit")

    def test_t9_t10_state_gate_marker_liveness_and_degradation_invariants(self):
        evidence = self._evidence()
        evidence[self.gen0["route_id"]]["plan-check"]["attempt_history"].extend([
            {"attempt_id": "pending", "status": "open", "pid": None, "contract_status": "current"},
            {"attempt_id": "killed", "status": "done", "pid": 501, "contract_status": "current",
             "note": "fleet-kill-cancel"},
            {"attempt_id": "dead", "status": "done", "pid": 502, "contract_status": "current",
             "note": "dead-worker-fail"},
        ])
        evidence[self.gen1["route_id"]]["plan-check"]["attempt_history"] = [
            {"attempt_id": "registered", "status": "open", "pid": None,
             "contract_status": "current"},
        ]
        evidence[self.gen1["route_id"]]["execute"] = {
            "status": "open",
            "attempt_history": [{"attempt_id": "att-degraded-execute", "status": "open",
                                  "pid": 503, "contract_status": "current"}],
        }
        views = route.build_views(self._jobs(), evidence, self.records, 100.0)
        view = next(view for view in views if view["route_id"] == self.gen1["route_id"])
        plan_check = next(node for node in view["nodes"] if node["id"] == "plan-check")
        self.assertIsNone(plan_check["attempt_round"])
        self.assertEqual(next(node for node in view["nodes"] if node["id"] == "plan")["revision_of"],
                         {"node": "plan-check", "round": 1})
        self.assertEqual(plan_check["state"], "pending")

        degraded = {self.gen1["route_id"]: [{
            "kind": "degradation", "dispatch_depth": 2, "route_node": "execute",
            "route_hash": self.gen1["route_hash"], "fallback_hop": "inline",
            "reason": "fleet_visibility=none", "fleet_visibility": "none",
            "registered_worker": 0, "ts": 900,
        }]}
        degraded_view = next(view for view in route.build_views(
            self._jobs(), evidence, self.records, 1000, degradations=degraded
        ) if view["route_id"] == self.gen1["route_id"])
        execute = next(node for node in degraded_view["nodes"] if node["id"] == "execute")
        self.assertEqual(execute["state"], "degraded")
        self.assertEqual(execute["attempt_round"], 2)
        self.assertEqual(execute["degradation"]["fallback_hop"], "inline")
        before = route._record_view(self.gen1, self.gen1["route_id"], self._jobs(),
                                    evidence[self.gen1["route_id"]], 100.0)
        scoped = next(view for view in views if view["route_id"] == self.gen1["route_id"])
        ignored = {"attempt_round", "route_attempt_round", "prior_attempt_rounds", "revision_of"}
        for left, right in zip(before["nodes"], scoped["nodes"]):
            self.assertEqual({k: v for k, v in left.items() if k not in ignored},
                             {k: v for k, v in right.items() if k not in ignored})

    def test_t11_context_scope_and_render_are_io_free(self):
        evidence = self._evidence()
        jobs = self._jobs()
        with mock.patch.object(projection, "_artifact_reader", return_value=None), \
             mock.patch.object(projection.glob, "glob", side_effect=AssertionError("glob")), \
             mock.patch.object(os, "listdir", side_effect=AssertionError("listdir")), \
             mock.patch.object(os, "scandir", side_effect=AssertionError("scandir")), \
             mock.patch.object(Path, "glob", side_effect=AssertionError("Path.glob")), \
             mock.patch.object(Path, "iterdir", side_effect=AssertionError("Path.iterdir")):
            projection.attach_projections([], jobs, route_records=self.records,
                                          node_evidence=evidence, now=100.0,
                                          spec_markers={}, capability_groundings={})
        self.assertIsNone(projection._ROUND_SCOPE.get())
        view = jobs[0].work_projection._route_view["view"]
        with mock.patch.object(route, "load", side_effect=AssertionError("render reopened route")):
            render._route_card_l2(view, 120)
        self.assertIsNone(projection._ROUND_SCOPE.get())

    def test_t12_session_lineage_is_unique_and_unrelated_owner_is_ambiguous(self):
        session = Session(
            harness="codex", pid=701, cwd="/session", slug="main", session_id="sid-main",
            liveness="working")
        owner = DispatchJob(key="autopilot-code", slug="owner", depth=1, worker_type="owner",
                            attempt_id="att-fixture-owner", parent_sid="sid-main", liveness="working")
        child = self._job(self.gen1, parent_sid="sid-main")
        evidence = self._evidence()
        for rid in (self.gen0["route_id"], self.gen1["route_id"]):
            for item in evidence[rid].values():
                item["parent"] = "main"
        projection.attach_projections([session], [owner, child], route_records=self.records,
                                      node_evidence=evidence, now=100.0,
                                      spec_markers={}, capability_groundings={})
        self.assertEqual(session.work_projection.source, "route-exact")
        self.assertEqual(session.work_projection.route_id, self.gen1["route_id"])
        self.assertIsNone(session.work_projection.ambiguity)

        unrelated = deepcopy(self.gen0)
        unrelated["owner_attempt_id"] = "att-unrelated"
        self._reseal(unrelated)
        other = self._job(unrelated, parent_sid="sid-main")
        projection.attach_projections([session], [owner, child, other],
                                      route_records={**self.records, unrelated["route_id"]: unrelated},
                                      node_evidence=evidence, now=100.0,
                                      spec_markers={}, capability_groundings={})
        self.assertEqual(session.work_projection.ambiguity, "multiple-owner-routes")

    def test_t14_flow_and_branch_revision_evidence_fits_or_drops_as_a_unit(self):
        evidence = self._evidence()
        view = next(view for view in route.build_views(self._jobs(), evidence, self.records, 100.0)
                    if view["route_id"] == self.gen1["route_id"])
        for width in (168, 120, 100, 60, 20):
            with self.subTest(width=width):
                rows = render._route_card_l2(view, width)
                text = "\n".join("".join(token for token, _kind in row) for row in rows)
                self.assertTrue(all(render._dw("".join(token for token, _kind in row)) <= width
                                    for row in rows))
                if width > 20:
                    self.assertIn("plan-check R1 → plan R2", text)
                else:
                    self.assertNotIn("plan-check R1 → plan R2", text)

    def test_verified_lineage_unions_rounds_and_exposes_json(self):
        evidence = self._evidence()
        views = route.build_views(self._jobs(), evidence, self.records, 100.0)
        view = next(view for view in views if view["route_id"] == self.gen1["route_id"])
        plan = next(node for node in view["nodes"] if node["id"] == "plan")
        self.assertEqual(plan["attempt_round"], 2)
        self.assertEqual(plan["route_attempt_round"], 1)
        self.assertEqual(plan["prior_attempt_rounds"], 1)
        self.assertEqual(plan["revision_of"], {"node": "plan-check", "round": 1})
        self.assertEqual(view["lineage"]["status"], "verified")
        self.assertEqual(view["lineage"]["chain"], [self.gen0["route_id"]])
        summary = route.summary(views)
        summary_plan = next(item for item in summary if item["route_id"] == self.gen1["route_id"])["nodes"][0]
        self.assertEqual(summary_plan["attempt_round"], 2)
        self.assertEqual(summary_plan["route_attempt_round"], 1)
        self.assertIn("lineage", next(item for item in summary if item["route_id"] == self.gen1["route_id"]))
        jobs = self._jobs()
        projection.attach_projections([], jobs, route_records=self.records,
                                      node_evidence=evidence, now=100.0)
        live_summary = projection.route_summary_from_projections(jobs)
        self.assertEqual(live_summary[0]["lineage"]["status"], "verified")

    def test_hash_mismatch_is_typed_and_keeps_route_local_round(self):
        broken = json.loads(json.dumps(self.gen1))
        broken["source_route_hash"] = "sha256:wrong"
        records = dict(self.records)
        records[broken["route_id"]] = broken
        view = next(view for view in route.build_views(self._jobs(), self._evidence(), records, 100.0)
                    if view["route_id"] == self.gen1["route_id"])
        self.assertEqual(view["lineage"]["reason"], "predecessor-hash-mismatch")
        plan = next(node for node in view["nodes"] if node["id"] == "plan")
        self.assertEqual(plan["attempt_round"], 1)
        self.assertIsNone(plan["revision_of"])

    def test_revision_text_is_fit_gated_without_glyph_or_state_changes(self):
        evidence = self._evidence()
        view = next(view for view in route.build_views(self._jobs(), evidence, self.records, 100.0)
                    if view["route_id"] == self.gen1["route_id"])
        wide = render._route_card_l2(view, 120)
        wide_text = "\n".join("".join(token for token, _kind in row) for row in wide)
        self.assertIn("plan-check R1 → plan R2", wide_text)
        narrow = render._route_card_l2(view, 20)
        narrow_text = "\n".join("".join(token for token, _kind in row) for row in narrow)
        self.assertNotIn("plan-check R1 → plan R2", narrow_text)
        self.assertTrue(all(render._dw("".join(token for token, _kind in row)) <= 20
                            for row in narrow))

    def test_collector_history_is_additive(self):
        with tempfile.TemporaryDirectory() as tmp:
            for name in ("gen0.json", "gen1.json"):
                shutil.copyfile(os.path.join(FIXTURE_DIR, name), os.path.join(tmp, name))
            registry = os.path.join(tmp, "registry.tsv")
            with open(os.path.join(FIXTURE_DIR, "registry.tsv"), encoding="utf-8") as source:
                text = source.read().replace("{FIXTURE_DIR}", tmp)
            with open(registry, "w", encoding="utf-8") as target:
                target.write(text)
            evidence, _terminal = dispatch._scan_registry_evidence([registry])
        history = evidence[self.gen1["route_id"]]["plan"]["attempt_history"][-1]
        self.assertEqual(history["parent_attempt_id"], "att-fixture-owner")
        self.assertEqual(history["note"], "running")
        self.assertEqual(history["registry_order"], 4)


if __name__ == "__main__":
    unittest.main()
