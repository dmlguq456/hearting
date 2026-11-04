#!/usr/bin/env python3
"""Policy replay of actual 1010–1011 records; full-path replay uses the private snapshot.

The bundle is an anonymized field projection of frozen routes/rows/markers,
with synthetic workload paths and session IDs. SHA256 fields retain source
provenance; attempt, verdict and process-result relationships are preserved.
No provider, project writer or live dispatch registry is used by this suite.
"""
import json
from pathlib import Path
from types import SimpleNamespace
import unittest

import route_authority as A
import review_round_cap as C
from resource_run_registry import resource_never_started


class IncidentReplay(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.path = Path(__file__).with_name("fixtures") / "continuation-incidents-1011.json"
        cls.before = cls.path.read_bytes()
        cls.data = json.loads(cls.before)

    @classmethod
    def tearDownClass(cls):
        assert cls.path.read_bytes() == cls.before

    def route(self, rid):
        source = self.data["routes"][rid]
        self.assertEqual(len(source["sha256"]), 64)
        return source["data"]

    def row(self, aid):
        return next(r for r in self.data["rows"] if r["metadata"]["attempt_id"] == aid)

    def rounds(self, rid, node):
        return [(r["status"], r["metadata"]) for r in self.data["rows"]
                if r["metadata"].get("route_id") == rid and r["metadata"].get("route_node") == node]

    def choice(self, rid, node, action="round", rows=None, **kw):
        route = self.route(rid)
        return A.decide_continuation(route, action=action,
            node=next(n for n in route["nodes"] if n["id"] == node),
            rows=self.rounds(rid, node) if rows is None else rows, **kw)

    def test_494_pinned_opencode_start_retains_the_existing_route(self):
        route = self.route("rt-28da758897fd6bb9")
        self.assertEqual(A.sealed_pin_harness(route, worker_type="owner"), "opencode")
        self.assertEqual(A.decide_continuation(route, action="owner-start").state, "start")
        self.assertEqual(A.decide_continuation(route, action="owner-start", facts={"registered": True}).state, "existing")

        row = self.row("att-e772add8be459e9cee25043598c8266f")
        decision = A.decide_continuation(action="owner-input", status=row["status"], metadata=row["metadata"])
        self.assertEqual((decision.allowed, decision.state), (True, "retained"))

    def test_495_original_fail_hidden_by_owner_closure_gets_only_cap_plus_one(self):
        rid = "rt-45e771bbf2480365"
        rows = self.rounds(rid, "impl-review")[:2]
        self.assertEqual(rows[-1][1]["failure_class"], "fail")
        decision = self.choice(rid, "impl-review", rows=rows,
                               revisions=[{"answers": [rows[-1][1]["attempt_id"]]}])
        self.assertEqual((decision.allowed, decision.budget.round_kind, decision.budget.cap), (True, "closure-check", 2))

    def test_after_cap_owner_disposition_does_not_open_a_fourth_review(self):
        rid = "rt-45e771bbf2480365"
        self.assertEqual(self.choice(rid, "impl-review").state, "exhausted")
        self.assertTrue(self.choice(rid, "impl-review", action="owner-closure").allowed)
        self.assertEqual(self.choice(rid, "impl-review").budget.verdict_rounds, 3)

    def test_497_known_gpu_not_started_is_returned_to_the_same_owner(self):
        runs = [r for source in self.data["resources"].values() for r in source["data"]["runs"].values()
                if r["run_id"] in ('fixture-run-0949', 'fixture-run-0949__a1')]
        self.assertTrue(runs)
        for row in runs:
            self.assertTrue(resource_never_started(row))
            decision = A.decide_continuation(action="resource-failure", facts={"not_started": resource_never_started(row)})
            self.assertEqual((decision.allowed, decision.next_action), (True, "return-to-owner"))

    def test_498_handover_keeps_the_registered_parent_and_exact_binding(self):
        entry = self.data["handover"][0]["data"]
        self.assertEqual(entry["source"], "retire")
        row = self.row("att-e772add8be459e9cee25043598c8266f")["metadata"]
        binding = next(b for b in entry["bindings"] if b["attempt"] == row["attempt_id"])
        self.assertEqual((binding["parent"], binding["route"], binding["hash"]),
                         (row["parent_sid"], row["route_id"], row["route_hash"]))
        self.assertNotEqual(entry["sid"], row["parent_sid"])

    def test_502_superseded_pass_remains_history_while_owner_closure_is_allowed(self):
        markers = [m["data"] for p, m in self.data["markers"].items() if p.endswith("/impl-review.2.json")]
        self.assertEqual(markers[0]["state"], "superseded-by-upstream-revision")
        self.assertTrue(self.choice("rt-498f16453b7ee1f6", "impl-review", action="owner-closure").allowed)
        self.assertEqual(markers[0]["review_independence"], "independent")

    def test_507_stale_smoke_not_started_does_not_restart_or_end_the_owner(self):
        row = next(r for source in self.data["resources"].values() for r in source["data"]["runs"].values()
                   if r.get("launch_error", {}).get("type") == "CalledProcessError")
        self.assertIn("stale smoke input", row["launch_error"]["message"])
        self.assertEqual(A.decide_continuation(action="resource-failure",
            facts={"not_started": resource_never_started(row)}).next_action, "return-to-owner")

    def test_508_closure_pending_does_not_erase_report_or_the_owners_answer(self):
        row = self.row("att-127a03aa96ee5a345aa6f578a003c844")
        self.assertEqual(A.decide_continuation(action="owner-input", status=row["status"],
            metadata=row["metadata"]).state, "retained")
        self.assertTrue(any(p.endswith("/report.json") for p in self.data["markers"]))

    def test_511_registered_payload_survives_replacement_and_live_owner_is_reused(self):
        aid = "att-92320f4fbed9b486511e34466041bd900c41450e6aff4c9c"
        row, observation = self.row(aid), self.data["observations"][aid]
        self.assertEqual(observation["reason"], "registered-resources-preserved")
        self.assertEqual(len(observation["preserved"]), 3)
        self.assertTrue(A.decide_continuation(action="replacement", status=row["status"], metadata=row["metadata"],
            facts={"process": SimpleNamespace(**observation), "death_kind": "corrected"}).allowed)
        aid = "att-644fad7c762b227f03819c87df45a78748711bec27c4505b"
        row = self.row(aid)
        self.assertEqual(A.decide_continuation(action="owner", status=row["status"], metadata=row["metadata"],
            facts={"process": SimpleNamespace(**self.data["observations"][aid])}).next_action, "handback")

    def test_verdictless_recovery_keeps_limits_and_existing_commands(self):
        rid = "rt-498f16453b7ee1f6"
        self.assertEqual(self.choice(rid, "test").state, "verdictless-bound")
        self.assertTrue(self.choice(rid, "test", action="complete-inline").allowed)
        rows = self.rounds(rid, "test")
        self.assertEqual(self.choice(rid, "test", action="marker", facts={"site": "inline"}).census["closure_class"], "owner-run-verdictless")
        self.assertEqual(C.recovery_fields("pipeline-stage", "verdictless-bound")["recovery_surface"], "capability-route complete --inline")
        self.assertEqual(rows[0][1]["failure_class"], "fail")

    def test_reboot_recovery_retains_the_verdictless_count_and_old_fail(self):
        rid = "rt-498f16453b7ee1f6"
        reboot = self.row("att-17a78cf9343b7e8335f1b50a371c6bead06374590942c0de")
        self.assertEqual(C.classify_round_row(reboot["status"], reboot["metadata"], worker_type="stage"), "verdict-less")
        self.assertEqual(self.choice(rid, "test").state, "verdictless-bound")
        self.assertEqual(self.rounds(rid, "test")[0][1]["failure_class"], "fail")


if __name__ == "__main__":
    unittest.main()
