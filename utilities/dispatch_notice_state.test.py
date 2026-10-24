#!/usr/bin/env python3
"""H6/H7: stale obligations are retired, real completion and gates survive."""
import json
import builtins
import importlib.util
import os
from pathlib import Path
import tempfile
import time
from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
from unittest import mock

import dispatch_notice_state as notice
import dispatch_pending_delivery as pending
import dispatch_session_sweep as sweep
import dispatch_supervision as supervision
from route_identity import route_hash
from workflow_state import WorkflowLedger


class NoticeTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.jobs = self.root / "jobs.log"
        self.artifact = self.root / "review.json"
        self.artifact.write_text("{}")
        self.path = self.root / "route.json"
        self.route = {"route_id": "rt-notice", "nodes": [{"id": "frame",
            "continuation": {"kind": "human-gate", "gate": "review"}}]}
        self.route["route_hash"] = route_hash(self.route)
        self.path.write_text(json.dumps(self.route))
        self.meta = dict(attempt_id="att-owner", route_id="rt-notice", route_node="frame",
                         route_file=str(self.path), route_hash=self.route["route_hash"],
                         parent_sid="parent", parent_completion_delivery="claude-parent-runtime")
        self.write_row()

    def test_delivery_batch_membership_decision_table(self):
        base = dict(registry_id="/jobs/one.log", parent_sid="parent-a",
                    attempt_id="att-a", route_id="route-a",
                    route_hash_value="hash-a")
        # Positive: the two declared frame legs of one exact route.
        frame_a = notice.delivery_batch_key(**base, route_node="frame")
        frame_b = notice.delivery_batch_key(**{**base, "attempt_id": "att-b"},
                                             route_node="frame-alternative")
        self.assertEqual(frame_a, frame_b)
        # Positive: exact parent session + parent cycle; capabilities may differ.
        cycle_a = notice.delivery_batch_key(**base, parent_cycle_id="cycle-a")
        cycle_b = notice.delivery_batch_key(**{**base, "attempt_id": "att-b"},
                                             parent_cycle_id="cycle-a")
        self.assertEqual(cycle_a, cycle_b)
        # Positive: one existing sealed declaration listing both attempts.
        sealed_a = notice.delivery_batch_key(
            **base, sealed_batch_id="sealed-a", sealed_attempt_ids=["att-a", "att-b"])
        sealed_b = notice.delivery_batch_key(
            **{**base, "attempt_id": "att-b"}, sealed_batch_id="sealed-a",
            sealed_attempt_ids=["att-a", "att-b"])
        self.assertEqual(sealed_a, sealed_b)
        # Negative: consecutive route-free launches and a shared cycle across parents.
        self.assertIsNone(notice.delivery_batch_key(
            registry_id="/jobs/one.log", parent_sid="parent-a", attempt_id="att-a"))
        self.assertNotEqual(
            notice.delivery_batch_key(**base, parent_cycle_id="cycle-a"),
            notice.delivery_batch_key(**{**base, "parent_sid": "parent-b", "attempt_id": "att-b"},
                                       parent_cycle_id="cycle-a"))
        # A shared sealed string without the exact sealed membership is not authority.
        self.assertIsNone(notice.delivery_batch_key(
            **base, sealed_batch_id="same-string", sealed_attempt_ids=["att-other"]))

    def _success_batch_fixture(self, *, materialize_sibling=True):
        self.route["parent_cycle_id"] = "cycle-shared"
        self.route["route_hash"] = route_hash(self.route)
        self.path.write_text(json.dumps(self.route))
        self.jobs.write_text("")
        ids = ["att-batch-a", "att-batch-b"]
        delivery_ids = ["delivery-batch-a", "delivery-batch-b"]
        created = []
        for index, (attempt_id, delivery_id) in enumerate(zip(ids, delivery_ids)):
            receipt = {"schema_version": 2, "state": "ready", "parent_attempt_id": "",
                       "job_registry": str(self.jobs), "delivery_classification": "success",
                       "children": [{"attempt_id": attempt_id, "required_action": "advance-completed"}]}
            if index == 0 or materialize_sibling:
                created.append(pending.create(
                    self.root, delivery_id=delivery_id, recipient_kind="claude-parent-runtime",
                    recipient_key="parent", session_generation="", session_generation_supported="0",
                    attempt_ids=[attempt_id], parent_attempt_id="att-parent", route_id="rt-notice",
                    route_node="frame", receipt=receipt,
                    receipt_digest=pending._canonical_receipt_digest(receipt),
                    row_revisions={attempt_id: "revision-" + str(index)}))
            stamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            meta = {"attempt_id": attempt_id, "dispatch_depth": "1", "registered_worker": "1",
                    "worker_type": "frame", "parent_sid": "parent", "delivery_intent": "1",
                    "delivery_intent_at_ns": str(time.monotonic_ns()),
                    "delivery_id": delivery_id, "failure_class": "pass", "note": "completed-marker",
                    "route_file": str(self.path), "route_id": "rt-notice",
                    "route_hash": self.route["route_hash"], "route_node": "frame"}
            self.jobs.write_text(self.jobs.read_text() + stamp + "\tdone\t/r\t/w\ttask\t" +
                                 ",".join(f"{key}={value}" for key, value in meta.items()) + "\n")
        return created, delivery_ids

    def _resolve_success_batch(self, seed, now_ns=None):
        from types import SimpleNamespace
        with mock.patch("dispatch_attempt_policy.committed_outcome", return_value="succeeded"), \
             mock.patch("dispatch_contract.observed_attempt_liveness",
                        return_value=SimpleNamespace(state="terminal")):
            return notice.resolve_delivery_batch(self.jobs, seed, recipient_key="parent",
                                                 delivery_root=self.root, now_ns=now_ns)

    def test_resolver_excludes_acknowledged_sibling_from_sequential_continuation(self):
        records, ids = self._success_batch_fixture()
        prior = pending.claim(self.root, "parent", ids[1], claim_owner="prior", lease_seconds=30)
        pending.ack(self.root, "parent", ids[1], acked_by="prior-recipient")
        batch = self._resolve_success_batch(records[0])
        self.assertEqual(batch["delivery_ids"], (ids[0],))
        self.assertFalse(batch["held"])

    def test_missing_materialization_holds_only_for_wall_clock_grace(self):
        records, ids = self._success_batch_fixture(materialize_sibling=False)
        lines = self.jobs.read_text().splitlines()
        # Offset keeps now - age positive on a freshly booted host (monotonic
        # clock below 900 s), where a non-positive stamp is treated as missing.
        now = time.monotonic_ns() + 2000 * 1_000_000_000
        fields = lines[1].split("\t")
        for age, expected in ((899, True), (901, False)):
            metadata = fields[5].split(",")
            fields[5] = ",".join(
                "delivery_intent_at_ns=" + str(now - age * 1_000_000_000)
                if part.startswith("delivery_intent_at_ns=") else part
                for part in metadata)
            self.jobs.write_text(lines[0] + "\n" + "\t".join(fields) + "\n")
            seed = records[0]
            batch = self._resolve_success_batch(seed, now_ns=now)
            self.assertEqual(batch["held"], expected)
            self.assertEqual(batch["delivery_ids"], (ids[0],))
        fields = self.jobs.read_text().splitlines()[1].split("\t")
        fields[5] = ",".join("delivery_intent_at_ns=unreadable" if part.startswith(
            "delivery_intent_at_ns=") else part for part in fields[5].split(","))
        self.jobs.write_text(lines[0] + "\n" + "\t".join(fields) + "\n")
        self.assertFalse(self._resolve_success_batch(records[0], now_ns=now)["held"])

    def test_unreadable_route_binding_fails_open(self):
        records, _ids = self._success_batch_fixture(materialize_sibling=False)
        self.path.write_text("{}", encoding="utf-8")
        self.assertIsNone(self._resolve_success_batch(records[0]))

    def test_unreadable_sibling_record_fails_open_to_seed_delivery(self):
        records, ids = self._success_batch_fixture()
        pending.record_path(self.root, "parent", ids[1]).write_text("{broken", encoding="utf-8")
        batch = self._resolve_success_batch(records[0])
        self.assertFalse(batch["held"])
        self.assertEqual(batch["delivery_ids"], (ids[0],))

    def test_hyphenated_capability_route_fallback_is_loaded_once(self):
        original_import = builtins.__import__
        calls = []
        loader = mock.Mock()
        spec = SimpleNamespace(loader=loader)
        module = SimpleNamespace()
        notice._CAPABILITY_ROUTE_CACHE = None

        def import_without_capability_route(name, *args, **kwargs):
            if name == "capability_route":
                raise ImportError(name)
            return original_import(name, *args, **kwargs)

        try:
            with mock.patch("builtins.__import__", side_effect=import_without_capability_route), \
                    mock.patch.object(notice.importlib.util, "spec_from_file_location",
                                      side_effect=lambda *args: calls.append(args) or spec), \
                    mock.patch.object(notice.importlib.util, "module_from_spec", return_value=module):
                self.assertIs(notice._capability_route_reader(), module)
                self.assertIs(notice._capability_route_reader(), module)
            self.assertEqual(len(calls), 1)
            loader.exec_module.assert_called_once_with(module)
        finally:
            notice._CAPABILITY_ROUTE_CACHE = None

    def write_row(self):
        self.jobs.write_text("now\tdone\t/r\t/w\ttask\t" + ",".join(k+"="+v for k,v in self.meta.items()) + "\n")

    def close(self, proven=True):
        self.path.with_suffix(".outcome.json").write_text(json.dumps(dict(
            route_id="rt-notice", route_hash=self.route["route_hash"], terminal_gate_proven=proven)))

    def seed(self, action="inspect-done-failure"):
        receipt = {"schema_version": 2, "state": "attention", "parent_attempt_id": "att-owner",
            "job_registry": str(self.jobs), "delivery_classification": "attention", "children": [{
                "attempt_id": "att-owner", "status": "done", "readiness": "ready", "reason": str(self.artifact),
                "required_action": action, "harness": "claude", "delivery_classification": "attention"}]}
        return pending.create(self.root, delivery_id="delivery-notice", recipient_kind="claude-parent-runtime",
            recipient_key="parent", session_generation="", session_generation_supported="0",
            attempt_ids=["att-owner"], parent_attempt_id="att-owner", route_id="rt-notice", route_node="frame",
            receipt=receipt, receipt_digest=pending._canonical_receipt_digest(receipt), row_revisions={"att-owner": "1"})

    def journal(self, record, *, state="BLOCKED_HUMAN_GATE", delivery=None):
        path = WorkflowLedger("rt-notice", self.route["route_hash"], jobs=self.jobs).journal_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"workflow_state": state, "evidence": {"gate": "review",
            "artifact": str(self.artifact), "delivery": delivery or str(pending.record_path(
                self.root, "parent", record["delivery_id"]))}}) + "\n")
        return path

    def test_closed_blocked_route_stops_repeated_sd111_continue_request(self):
        self.seed()
        self.close(proven=False)
        self.assertEqual(sweep.sweep_deliver(self.root, "claude-parent-runtime", "parent")[0], [])
        record = pending.read(self.root, "parent", "delivery-notice")
        self.assertEqual(record["state"], "rejected")

    def test_closed_success_still_delivers_first_completion(self):
        self.seed("advance-completed")
        self.close()
        records, _ = sweep.sweep_deliver(self.root, "claude-parent-runtime", "parent")
        self.assertEqual(len(records), 1)
        self.assertEqual(sweep.ack_delivered(self.root, "parent", records, acked_by="test"), 1)
        self.assertEqual(sweep.sweep_deliver(self.root, "claude-parent-runtime", "parent")[0], [])

    def test_exact_already_received_completion_retires_only_its_duplicate_notice(self):
        record = self.seed("advance-completed")
        with mock.patch.object(notice, "framed_decision_consumed", return_value=False), \
             mock.patch.object(notice, "exact_completion_already_received", return_value=True):
            self.assertFalse(notice.notice_is_current(record, jobs=self.jobs))
        with mock.patch.object(notice, "framed_decision_consumed", return_value=False), \
             mock.patch.object(notice, "exact_completion_already_received", return_value=False):
            self.assertTrue(notice.notice_is_current(record, jobs=self.jobs))

    def test_route_wide_terminal_pass_does_not_consume_a_notice(self):
        route = {
            "route_id": "rt-notice", "capability": "route-frame",
            "effective_intensity": "standard",
            "selection": {"shape": "framed"},
            "nodes": [
                {"id": "frame"}, {"id": "frame-alternative"},
                {"id": "route-decision", "kind": "runtime-terminal", "terminal": True},
            ],
        }
        route["route_hash"] = route_hash(route)
        self.path.write_text(json.dumps(route))
        self.route = route
        self.meta.update(route_id=route["route_id"], route_hash=route["route_hash"],
                         parent_attempt_id="att-owner")
        self.write_row()
        record = self.seed("advance-completed")
        reader = SimpleNamespace(terminal_gate_observation=lambda *a, **k: {
            "route-decision": {"passed": True},
        })
        import sys
        with mock.patch.dict(sys.modules, {"capability_route": reader}):
            self.assertFalse(notice.framed_decision_consumed(record, self.meta, self.jobs))
            self.assertTrue(notice.notice_is_current(record, jobs=self.jobs))
            child_meta = dict(self.meta, attempt_id="att-child", parent_attempt_id="att-owner")
            self.assertFalse(notice.framed_decision_consumed(record, child_meta, self.jobs))
            child_meta["parent_attempt_id"] = "att-another-recipient"
            self.assertFalse(notice.framed_decision_consumed(record, child_meta, self.jobs))

    def test_framed_decision_does_not_consume_foreign_attempt_or_recipient(self):
        route = {
            "route_id": "rt-notice", "capability": "route-frame",
            "effective_intensity": "standard", "selection": {"shape": "framed"},
            "nodes": [{"id": "frame"}, {"id": "frame-alternative"},
                      {"id": "route-decision", "kind": "runtime-terminal", "terminal": True}],
        }
        route["route_hash"] = route_hash(route)
        self.path.write_text(json.dumps(route))
        self.route = route
        self.meta.update(route_id=route["route_id"], route_hash=route["route_hash"],
                         parent_attempt_id="att-owner")
        self.write_row()
        record = self.seed("advance-completed")
        # Regress the pre-fix seam in isolation: a route-wide PASS used to retire
        # a notice whose immutable recipient and attempt set belong elsewhere.
        record = dict(record, attempt_ids=["att-foreign"], recipient_digest=pending.recipient_digest("other"))
        reader = SimpleNamespace(terminal_gate_observation=lambda *a, **k: {
            "route-decision": {"passed": True},
        })
        import sys
        with mock.patch.dict(sys.modules, {"capability_route": reader}):
            self.assertTrue(notice.notice_is_current(record, jobs=self.jobs))

    def test_framed_decision_needs_exact_recipient_attempt_set_and_route(self):
        route = {
            "route_id": "rt-notice", "capability": "route-frame",
            "effective_intensity": "standard", "selection": {"shape": "framed"},
            "nodes": [{"id": "frame"}, {"id": "frame-alternative"},
                      {"id": "route-decision", "kind": "runtime-terminal", "terminal": True}],
        }
        route["route_hash"] = route_hash(route)
        self.path.write_text(json.dumps(route))
        self.route = route
        self.meta.update(route_id=route["route_id"], route_hash=route["route_hash"],
                         parent_attempt_id="att-owner")
        self.write_row()
        record = self.seed("advance-completed")
        reader = SimpleNamespace(terminal_gate_observation=lambda *a, **k: {
            "route-decision": {"passed": True, "current": True},
        })
        import sys
        with mock.patch.dict(sys.modules, {"capability_route": reader}):
            for label, changed in (
                ("foreign attempt", dict(record, attempt_ids=["att-foreign", "att-other"])),
                ("foreign recipient", dict(record, recipient_digest=pending.recipient_digest("other"))),
            ):
                with self.subTest(label):
                    self.assertTrue(notice.notice_is_current(changed, jobs=self.jobs))
            self.assertTrue(notice.notice_is_current(
                record, jobs=self.jobs))  # the stored digest is for parent_sid="parent"
            foreign_session = dict(self.meta, parent_sid="other")
            self.assertFalse(notice.framed_decision_consumed(record, foreign_session, self.jobs))
            # A gate reader that claims PASS but reports a different digest for
            # its evidence cannot consume this notice.
            reader.terminal_gate_observation = lambda *a, **k: {
                "route-decision": {"passed": True, "current": True,
                                   "evidence": str(self.artifact), "evidence_digest": "0" * 64},
            }
            self.assertTrue(notice.notice_is_current(record, jobs=self.jobs))
            foreign_route = dict(record, route_id="rt-other")
            pending.claim(self.root, "parent", record["delivery_id"], claim_owner="owner", lease_seconds=60)
            self.assertFalse(notice.keep_claim(self.root, "parent", record["delivery_id"],
                                               foreign_route, "owner", jobs=self.jobs))
            self.assertEqual(pending.read(self.root, "parent", record["delivery_id"])["state"], "claimed")

    def test_real_route_decision_artifact_digest_drift_is_not_consumption(self):
        source = Path(__file__).with_name("framed_route.test.py")
        spec = importlib.util.spec_from_file_location("notice_framed_route_fixture", source)
        framed = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(framed)
        case = framed.FramedEndingTest(
            "test_proceed_with_no_proposal_fixes_a_none_record_and_ends_the_route")
        case.setUp()
        try:
            ending = case.settle()
            evidence = Path(ending["record_file"])
            self.assertTrue(evidence.is_file())
            before = framed.R.terminal_gate_observation(case.route, jobs=case.jobs)
            self.assertTrue(before["route-decision"]["passed"])
            marker_path = framed.R.completion_dir(case.route["route_id"], jobs=case.jobs) / "route-decision.json"
            marker = json.loads(marker_path.read_text())
            changed = evidence.read_bytes() + b"\n"
            evidence.write_bytes(changed)
            # The real reader may reject the drift immediately or still expose
            # the marker row in its non-exact mode; neither outcome is enough
            # for the exact consumer proof below.
            framed.R.terminal_gate_observation(case.route, jobs=case.jobs)
            import dispatch_contract
            self.assertNotEqual(dispatch_contract.evidence_digest(evidence), marker["evidence"]["sha256"])
            record = {"parent_attempt_id": "att-parent", "attempt_ids": ["att-a", "att-b"],
                      "recipient_digest": pending.recipient_digest("parent"),
                      "route_id": case.route["route_id"]}
            metadata = {"attempt_id": "att-parent", "route_id": case.route["route_id"],
                        "route_hash": case.route["route_hash"], "route_file": str(case.path),
                        "parent_sid": "parent"}
            self.assertFalse(notice.framed_decision_consumed(record, metadata, case.jobs))
        finally:
            case.doCleanups()

    def test_materialized_single_frame_notice_stops_after_exact_decision(self):
        import dispatch_completion_join as join
        from dispatch_contract import process_start_ticks
        # The synthetic gone workers use this host's actual birth clock.
        fixture_start = process_start_ticks(os.getpid())
        self.assertIsNotNone(fixture_start)
        source = Path(__file__).with_name("framed_route.test.py")
        spec = importlib.util.spec_from_file_location("notice_production_frame_fixture", source)
        framed = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(framed)
        case = framed.FramedEndingTest(
            "test_proceed_with_no_proposal_fixes_a_none_record_and_ends_the_route")
        case.setUp()
        try:
            records = []
            for node in case.route["nodes"][:2]:
                attempt = "att-notice-" + node["id"]
                metadata = dict(
                    attempt_schema_version="2", dispatch_depth="1", transport="headless",
                    execution_surface="registered-headless", registered_worker="1",
                    fallback_hop="same-harness-headless", worker_type="frame",
                    attempt_id=attempt, route_id=case.route["route_id"],
                    route_hash=case.route["route_hash"], route_file=str(case.path),
                    route_node=node["id"], parent_sid="parent", harness="claude",
                    parent_completion_delivery="claude-parent-runtime",
                    launch_claimed="1", launch_started="1", pid="99999999", pid_start=fixture_start,
                    pgid="99999999", pid_scope="host-visible", process_exit="0",
                    launch_lifecycle="foreground-scoped", launch_outcome="governed-process-reaped",
                    group_reap_proof="pgid-empty-v1", group_reap_pgid="99999999",
                    note="completed-supervisor", failure_class="pass")
                line = "now\topen\t/r\t/w\t" + node["id"] + "\t" + ",".join(
                    key + "=" + value for key, value in metadata.items()) + "\n"
                with case.jobs.open("a") as stream:
                    stream.write(line)
                framed.R.complete_node(case.route, node, node["id"],
                    case.output / "shards" / node["id"] / "direction-brief.md",
                    jobs=case.jobs, attempt_id=attempt)
                fields = next(line.split("\t") for line in case.jobs.read_text().splitlines()
                              if join.parse_registry_metadata(line.split("\t")[-1]).get("attempt_id") == attempt)
                path = join.materialize_pending_delivery(case.jobs, fields)
                self.assertIsNotNone(path)
                record = json.loads(path.read_text())
                self.assertEqual(record["attempt_ids"], [attempt])
                self.assertEqual(record["parent_attempt_id"], "-")
                self.assertTrue(notice.notice_is_current(record, jobs=case.jobs))
                records.append(record)
            claimed, _ = sweep.sweep_deliver(case.jobs.parent, "claude-parent-runtime", "parent")
            self.assertEqual(len(claimed), 2)
            for record in claimed:
                pending.mark_sent_ambiguous(case.jobs.parent, "parent", record["delivery_id"],
                                            claim_owner=record["claim_owner"])
            case.settle()
            for record in records:
                self.assertFalse(notice.notice_is_current(record, jobs=case.jobs))
                for change in (
                    {"attempt_ids": ["att-unconsumed"]},
                    {"recipient_digest": pending.recipient_digest("other")},
                    {"parent_attempt_id": "att-other"},
                    {"route_node": "execute"},
                ):
                    with self.subTest(change=change):
                        self.assertTrue(notice.notice_is_current(dict(record, **change), jobs=case.jobs))
            late = max(record["claim_deadline_ns"] for record in claimed) + 8 * 60 * 10**9
            self.assertEqual(sweep.sweep_deliver(case.jobs.parent, "claude-parent-runtime", "parent",
                                              now_ns=late)[0], [])
            for record in records:
                stored = pending.read(case.jobs.parent, "parent", record["delivery_id"])
                self.assertEqual(stored["state"], "rejected")
            # The existing attempts counter counts claims, including the
            # reclaim that retires an obsolete notice. It is not an emit
            # count; the carrier returned no second notification above.
            before = [pending.read(case.jobs.parent, "parent", record["delivery_id"])
                      for record in records]
            self.assertEqual(sweep.sweep_deliver(case.jobs.parent, "claude-parent-runtime", "parent",
                                              now_ns=late + 8 * 60 * 10**9)[0], [])
            self.assertEqual(before, [pending.read(case.jobs.parent, "parent", record["delivery_id"])
                                      for record in records])
        finally:
            case.doCleanups()

    def test_bad_closure_is_unknown_and_preserves_claim_for_recovery(self):
        self.seed()
        self.path.with_suffix(".outcome.json").write_text('{"route_id":"another"}')
        self.assertEqual(sweep.sweep_deliver(self.root, "claude-parent-runtime", "parent")[0], [])
        self.assertEqual(pending.read(self.root, "parent", "delivery-notice")["state"], "claimed")

    def test_closed_route_retires_exited_supervisor_during_pending_settlement(self):
        from types import SimpleNamespace
        self.meta["workflow_completion"] = "runtime-v1"
        self.write_row()
        record = supervision.materialize(self.jobs, {"att-owner"}, reason="supervisor-exited")[0]
        self.close()
        with mock.patch("dispatch_terminal_commit.owner_completion_pending", return_value=True), \
             mock.patch("dispatch_contract.attempt_process_quiescence",
                        return_value=SimpleNamespace(state="quiescent", reason="registry-closed")):
            self.assertEqual(record["receipt"]["reason"], "supervisor-exited")
            self.assertEqual(record["receipt"]["obligation_revision"], "")
            self.assertFalse(notice.notice_is_current(record))
            # A real finishing failure is not silenced by an early close.
            blocked = supervision.materialize(self.jobs, {"att-owner"}, reason="closure-blocked")[0]
            self.assertEqual(blocked["receipt"]["reason"], "closure-blocked")
            self.assertTrue(notice.notice_is_current(blocked))

    def test_gate_needs_latest_exact_raise_but_can_outlive_owner(self):
        record = self.seed("human-gate:review")
        self.journal(record)
        self.assertTrue(notice.notice_is_current(record))  # owner row is already done
        path = self.journal(record, delivery="/another/delivery.json")
        with self.assertRaisesRegex(ValueError, "publication-pending"):
            notice.notice_is_current(record)
        self.journal(record)
        with path.open("a") as stream:
            stream.write(json.dumps({"workflow_state": "RUNNING", "evidence": {"released_gate": "review"}}) + "\n")
        self.assertFalse(notice.notice_is_current(record))
        self.journal(record)
        self.close()
        self.assertFalse(notice.notice_is_current(record))

    def test_corrupt_gate_journal_is_not_silently_consumed(self):
        record = self.seed("human-gate:review")
        self.journal(record).write_text("corrupt\n")
        self.assertEqual(sweep.sweep_deliver(self.root, "claude-parent-runtime", "parent")[0], [])
        self.assertEqual(pending.read(self.root, "parent", "delivery-notice")["state"], "claimed")

    def test_missing_gate_artifact_defers_without_discarding_obligation(self):
        record = self.seed("human-gate:review")
        self.journal(record)
        self.artifact.unlink()
        self.assertEqual(sweep.sweep_deliver(self.root, "claude-parent-runtime", "parent")[0], [])
        self.assertEqual(pending.read(self.root, "parent", "delivery-notice")["state"], "claimed")

    def publication_race(self, *, reissue=False):
        record = self.seed("human-gate:review")
        path = self.journal(record, delivery="/previous/delivery.json")
        prior = path.read_text() if reissue else ""
        prior += json.dumps({"workflow_state": "RUNNING", "evidence": {"released_gate": "review"}}) + "\n"
        path.write_text(prior)
        self.assertEqual(sweep.sweep_deliver(self.root, "claude-parent-runtime", "parent")[0], [])
        claimed = pending.read(self.root, "parent", "delivery-notice")
        self.assertEqual(claimed["state"], "claimed")
        self.journal(record)
        path.write_text(prior + path.read_text())
        records, _ = sweep.sweep_deliver(self.root, "claude-parent-runtime", "parent",
                                        now_ns=claimed["claim_deadline_ns"] + 1)
        self.assertEqual([r["delivery_id"] for r in records], [record["delivery_id"]])

    def test_first_gate_published_after_sweep_remains_deliverable(self):
        self.publication_race()

    def test_reissued_gate_does_not_inherit_previous_release(self):
        self.publication_race(reissue=True)

    def test_exact_old_release_remains_resolved_after_next_raise(self):
        record = self.seed("human-gate:review")
        path = self.journal(record)
        prior = path.read_text() + json.dumps({"workflow_state": "RUNNING", "evidence": {"released_gate": "review"}}) + "\n"
        self.journal(record, delivery="/new/delivery.json")
        path.write_text(prior + path.read_text())
        self.assertFalse(notice.notice_is_current(record))


if __name__ == "__main__":
    unittest.main()
