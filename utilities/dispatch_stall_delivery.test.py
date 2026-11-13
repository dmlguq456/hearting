#!/usr/bin/env python3
"""Closed-loop fixtures for advisory attempt stalls and exact parent receipts."""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import importlib.util
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "utilities"))
sys.path.insert(0, str(ROOT / "tools"))
SPEC = importlib.util.spec_from_file_location("peer_steward_stall_fixture",
                                              ROOT / "utilities/peer-steward.py")
peer_steward = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(peer_steward)
import peer_session_fixture  # noqa: E402  (the mocked `agent list` is the live-session table)
peer_session_fixture.install(peer_steward)

import dispatch_contract
import dispatch_pending_delivery as pending
import dispatch_supervision as supervision
import peer_obligations
from harness_capabilities import parent_completion
from fleet.collectors.dispatch import _attempt_stall_attention
from fleet.collectors import dispatch as fleet_dispatch
from fleet import render as fleet_render
from fleet import route as fleet_route
from fleet.model import DispatchJob


class StallDeliveryLoopTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="stall-delivery-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.jobs = self.root / "jobs.log"
        self.attempt = "att-stall-loop"
        self.parent = "idle-parent-fixture"

    def _row(self, harness):
        carrier = parent_completion(harness)["carrier"]
        metadata = {
            "attempt_schema_version": "2", "dispatch_depth": "2",
            "transport": "headless", "execution_surface": "registered-headless",
            "registered_worker": "1", "fallback_hop": "same-harness-headless",
            "worker_type": "stage", "harness": harness,
            "route_id": "rt-stall", "route_hash": "sha256:" + "a" * 64,
            "route_node": "execute", "attempt_id": self.attempt,
            "parent_sid": self.parent, "parent_harness": harness,
            "parent_completion_delivery": carrier, "managed_sealed_batch_id": "",
            "pid_ns": dispatch_contract.process_namespace_identity() or "",
            "pid_observer_ns": dispatch_contract.process_namespace_identity() or "",
            "pid_scope": "host-visible",
        }
        self.jobs.write_text("2026-10-26T00:00:00Z\topen\t/repo\t" + str(self.root)
            + "\texecute\t" + ",".join(f"{key}={value}" for key, value in metadata.items()) + "\n",
            encoding="utf-8")
        return metadata, carrier

    def _materialize_stall(self, harness):
        metadata, carrier = self._row(harness)
        with mock.patch.dict(os.environ, {
                "XDG_STATE_HOME": str(self.root / "xdg-state"),
                "AGENT_PEER_LEDGER_ROOT": str(self.root / "peer-state")}), \
             mock.patch.object(supervision, "_attempt_human_wait", return_value=False):
            observation = supervision.update_attempt_stall_observation(
                self.jobs, self.attempt, now=100.0, row=("open", metadata),
                worktree=str(self.root))
        self.assertEqual(observation["state"], "stalled")
        before_registry = self.jobs.read_bytes()
        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(self.root / "xdg-state"),
                                          "AGENT_PEER_LEDGER_ROOT": str(self.root / "peer-state")}), \
             mock.patch.object(dispatch_contract, "attempt_execution_observation",
                return_value=dispatch_contract.AttemptExecutionObservation(
                    "absent", "carrier-only")), \
             mock.patch.object(supervision, "_attempt_human_wait", return_value=False), \
             mock.patch("peer_obligations.ensure_normal_runner"):
            records = supervision.materialize(self.jobs, {self.attempt}, reason="attempt-stalled")
        self.assertEqual(self.jobs.read_bytes(), before_registry)
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record["recipient_kind"], carrier)
        self.assertEqual(record["recipient_digest"], pending.recipient_digest(self.parent))
        self.assertEqual(record["state"], "pending")
        self.assertEqual(record["receipt"]["monitored_attempt_ids"], [self.attempt])
        with mock.patch.object(supervision, "_attempt_human_wait", return_value=False):
            self.assertTrue(supervision.notice_is_current(record))
        duty_id = peer_obligations.stable_duty_id("registered-batch", {
            "jobs": str(self.jobs.resolve()), "attempt_id": self.attempt,
            "session_id": self.parent, "harness": harness, "server": "default",
        }, record["delivery_id"])
        duty = peer_obligations.ObligationStore(self.jobs.parent).get(duty_id)
        self.assertEqual(duty["intent"]["notice_delivery_id"], record["delivery_id"])
        self.assertEqual(duty["intent"]["notice_digest"], record["receipt_digest"])
        return metadata, carrier, record, duty

    def _materialize_nested_stall(self, harness):
        self.owner_attempt = "att-stall-owner-" + harness
        self.attempt = "att-stall-child-" + harness
        self.parent = "idle-parent-" + harness
        self.immediate_parent = "owner-session-" + harness
        carrier = parent_completion(harness)["carrier"]
        common = {
            "attempt_schema_version": "2", "transport": "headless",
            "execution_surface": "registered-headless", "registered_worker": "1",
            "fallback_hop": "same-harness-headless", "harness": harness,
            "route_id": "rt-nested-stall", "route_hash": "sha256:" + "b" * 64,
            "parent_harness": harness,
            "pid_ns": dispatch_contract.process_namespace_identity() or "",
            "pid_observer_ns": dispatch_contract.process_namespace_identity() or "",
            "pid_scope": "host-visible",
        }
        owner = {**common, "dispatch_depth": "1", "worker_type": "owner",
            "unit": "_kernel/owner", "route_node": "_owner",
            "attempt_id": self.owner_attempt, "parent_sid": self.parent,
            "parent_completion_delivery": carrier, "managed_sealed_batch_id": ""}
        child = {**common, "dispatch_depth": "2", "worker_type": "stage",
            "unit": "dev/backend", "route_node": "execute",
            "attempt_id": self.attempt, "parent_attempt_id": self.owner_attempt,
            "parent_sid": self.immediate_parent,
            "parent_completion_delivery": carrier, "managed_sealed_batch_id": ""}
        lines = []
        for slug, metadata in (("owner", owner), ("child", child)):
            lines.append("2026-10-26T00:00:00Z\topen\t/repo\t" + str(self.root)
                + "\t" + slug + "\t" + ",".join(f"{key}={value}"
                    for key, value in metadata.items()))
        self.jobs.write_text("\n".join(lines) + "\n", encoding="utf-8")
        before_registry = self.jobs.read_bytes()
        with mock.patch.dict(os.environ, {
                "XDG_STATE_HOME": str(self.root / "xdg-state"),
                "AGENT_PEER_LEDGER_ROOT": str(self.root / "peer-state")}), \
             mock.patch.object(supervision, "_attempt_human_wait", return_value=False):
            observation = supervision.update_attempt_stall_observation(
                self.jobs, self.attempt, now=100.0, row=("open", child),
                worktree=str(self.root))
        self.assertEqual(observation["state"], "stalled")
        with mock.patch.dict(os.environ, {
                "XDG_STATE_HOME": str(self.root / "xdg-state"),
                "AGENT_PEER_LEDGER_ROOT": str(self.root / "peer-state")}), \
             mock.patch.object(dispatch_contract, "attempt_execution_observation",
                return_value=dispatch_contract.AttemptExecutionObservation(
                    "absent", "carrier-only")), \
             mock.patch.object(supervision, "_attempt_human_wait", return_value=False), \
             mock.patch("peer_obligations.ensure_normal_runner"):
            records = supervision.materialize(
                self.jobs, {self.attempt}, reason="attempt-stalled")
        self.assertEqual(self.jobs.read_bytes(), before_registry)
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record["recipient_kind"], carrier)
        self.assertEqual(record["recipient_digest"], pending.recipient_digest(self.parent))
        self.assertEqual(record["receipt"]["owner_attempt_id"], self.owner_attempt)
        self.assertEqual(record["receipt"]["monitored_attempt_ids"], [self.attempt])
        self.assertEqual(record["receipt"]["recipient_thread_id"], self.parent)
        with mock.patch.object(supervision, "_attempt_human_wait", return_value=False):
            self.assertTrue(supervision.notice_is_current(record))
        duty_id = peer_obligations.stable_duty_id("registered-batch", {
            "jobs": str(self.jobs.resolve()), "attempt_id": self.owner_attempt,
            "session_id": self.parent, "harness": harness, "server": "default",
        }, record["delivery_id"])
        duty = peer_obligations.ObligationStore(self.jobs.parent).get(duty_id)
        self.assertEqual(duty["intent"]["notice_delivery_id"], record["delivery_id"])
        return child, record

    def _collect_fleet_jobs(self):
        with mock.patch.object(fleet_dispatch, "_scan_processes", return_value=[]), \
             mock.patch.object(fleet_dispatch, "_live_attempt_ids", return_value=set()), \
             mock.patch.object(fleet_dispatch, "_dispatch_liveness", return_value="working"), \
             mock.patch.object(fleet_dispatch, "_note_home_view", return_value=True), \
             mock.patch.object(fleet_dispatch, "_build_codex_rollout_index", return_value=None), \
             mock.patch.object(fleet_dispatch, "_reconcile_drill_rows", side_effect=lambda jobs, *a, **k: jobs), \
             mock.patch.object(supervision, "_attempt_human_wait", return_value=False), \
             mock.patch.object(fleet_dispatch, "_enrich_claude_stream_session"), \
             mock.patch.object(fleet_dispatch, "_enrich_codex_attempt_session"), \
             mock.patch.object(fleet_dispatch, "_enrich_opencode_attempt_session"), \
             mock.patch.object(fleet_dispatch, "_enrich_attempt_summary"):
            return fleet_dispatch.collect(jobs_path=str(self.jobs), fast_first=True)

    def test_nested_child_notice_projects_through_fleet_and_clears_after_recovery(self):
        child_metadata, record = self._materialize_nested_stall("codex")
        jobs = self._collect_fleet_jobs()
        child_job = next(job for job in jobs if job.attempt_id == self.attempt)
        self.assertEqual(child_job.parent_sid, self.immediate_parent)
        self.assertEqual(child_job.stall_observation["delivery_id"], record["delivery_id"])
        node = fleet_route._node_state("execute", [child_job], {}, now=200.0)
        self.assertEqual(node["state"], "attention")
        text, _color, _mark = fleet_render._route_node_text({"id": "execute", **node})
        self.assertIn("! stalled", text)

        with mock.patch.object(dispatch_contract, "attempt_execution_observation",
                return_value=dispatch_contract.AttemptExecutionObservation(
                    "active", "runtime-command-live")), \
             mock.patch.object(supervision, "_attempt_human_wait", return_value=False):
            recovered = supervision.update_attempt_stall_observation(
                self.jobs, self.attempt, now=200.0, row=("open", child_metadata),
                worktree=str(self.root))
        self.assertEqual(recovered["state"], "active")
        recovered_jobs = self._collect_fleet_jobs()
        recovered_child = next(job for job in recovered_jobs if job.attempt_id == self.attempt)
        self.assertIsNone(recovered_child.stall_observation)
        recovered_node = fleet_route._node_state(
            "execute", [recovered_child], {}, now=300.0)
        self.assertEqual(recovered_node["state"], "active")
        recovered_text, _color, _mark = fleet_render._route_node_text(
            {"id": "execute", **recovered_node})
        self.assertNotIn("! stalled", recovered_text)

    def test_nested_fleet_binding_rejects_wrong_member_digest_and_recipient(self):
        _child, record = self._materialize_nested_stall("codex")
        state_path = self.root / "watchdog" / f"{self.attempt}.json"
        original_bytes = state_path.read_bytes()
        original_state = json.loads(original_bytes)
        original_stall = original_state["stall_observation"]
        import dispatch_supervision

        def collect_with(stall, retained_record, *, current=True, reader=None):
            state = dict(original_state)
            state["stall_observation"] = stall
            state_path.write_text(json.dumps(state), encoding="utf-8")
            read = reader or (lambda *_args, **_kwargs: retained_record)
            with mock.patch("dispatch_pending_delivery.read", side_effect=read), \
                 mock.patch.object(dispatch_supervision, "notice_is_current",
                                   return_value=current):
                jobs = self._collect_fleet_jobs()
            return next(job for job in jobs if job.attempt_id == self.attempt)

        try:
            wrong_member = copy.deepcopy(record)
            wrong_member["receipt"]["monitored_attempt_ids"] = ["att-other-member"]
            wrong_member["receipt_digest"] = supervision.receipt_digest(wrong_member["receipt"])
            stall = dict(original_stall, notice_digest=wrong_member["receipt_digest"])
            self.assertIsNone(collect_with(stall, wrong_member).stall_observation)

            wrong_digest = copy.deepcopy(record)
            wrong_digest["receipt_digest"] = "sha256:" + "0" * 64
            self.assertIsNone(collect_with(original_stall, wrong_digest).stall_observation)

            wrong_recipient_stall = dict(original_stall,
                delivery_recipient_thread_id=self.immediate_parent,
                delivery_recipient_digest=pending.recipient_digest(self.immediate_parent))
            requested = []
            def exact_key_read(root, recipient, delivery_id):
                requested.append(recipient)
                return record
            self.assertIsNone(collect_with(
                wrong_recipient_stall, record, reader=exact_key_read).stall_observation)
            self.assertEqual(requested, [self.immediate_parent])
        finally:
            state_path.write_bytes(original_bytes)

    def test_observation_retention_courier_exact_receive_and_recovery_for_each_adapter(self):
        for harness in ("claude", "codex", "opencode"):
            with self.subTest(harness=harness), tempfile.TemporaryDirectory(
                    prefix=f"stall-{harness}-") as root:
                self.root = Path(root)
                self.jobs = self.root / "jobs.log"
                self.attempt = "att-stall-loop-" + harness
                self.parent = "idle-parent-" + harness
                metadata, carrier, record, duty = self._materialize_stall(harness)
                store = peer_obligations.ObligationStore(self.jobs.parent)
                stall_state_path = self.root / "watchdog" / f"{self.attempt}.json"
                initial_episode = json.loads(stall_state_path.read_text(encoding="utf-8"))[
                    "stall_observation"]["episode_id"]
                expected = (harness, self.parent)
                fleet_job = DispatchJob(key="code", route_id=metadata["route_id"],
                    route_hash=metadata["route_hash"], route_node=metadata["route_node"],
                    attempt_id=self.attempt, parent_sid=self.parent,
                    worker_type="stage", status="open", liveness="working")
                fleet_job._registry_path = str(self.jobs)
                fleet_job._launch_home = str(self.root)
                census = {"result": {"agents": [{"agent": harness, "pane_id": "w1:p5",
                    "agent_session": {"value": self.parent}}]}}
                peer_root = self.root / "peer-state"

                def receive(args, *, expected_recipient, on_prepared, producer_binding):
                    self.assertEqual(expected_recipient, expected)
                    body = Path(args.body_file).read_text(encoding="utf-8")
                    sender = {"harness": "runtime", "session_id": None,
                              "name": "Hearting runtime"}
                    recipient = {"harness": harness, "session_id": self.parent}
                    text, ref = peer_steward.peer_message.prepare_peer_message(
                        body, sender, recipient, defer=True, refs=list(args.ref),
                        producer_binding=producer_binding)
                    on_prepared(ref)
                    self.assertEqual(peer_steward.peer_message.receive_peer_message(
                        text, recipient), 0)
                    return 0

                with mock.patch.dict(os.environ, {"AGENT_PEER_LEDGER_ROOT": str(peer_root),
                                                  "XDG_STATE_HOME": str(self.root / "xdg-state")}), \
                     mock.patch.object(dispatch_contract, "attempt_execution_observation",
                        return_value=dispatch_contract.AttemptExecutionObservation(
                            "absent", "carrier-only")), \
                     mock.patch.object(supervision, "_attempt_human_wait", return_value=False), \
                     mock.patch.object(peer_steward.subprocess, "run",
                        return_value=SimpleNamespace(stdout=json.dumps(census), returncode=0)), \
                     mock.patch.object(peer_steward, "_resolve_target", return_value=expected), \
                     mock.patch.object(peer_steward, "cmd_prompt", side_effect=receive), \
                     mock.patch.object(peer_steward, "_settle_claude_native_dispatch_receipt",
                        return_value=False):
                    self.assertTrue(supervision.notice_is_current(record))
                    state_path = self.root / "watchdog" / f"{self.attempt}.json"
                    before_fleet = state_path.read_bytes()
                    self.assertEqual(_attempt_stall_attention(fleet_job)["delivery_id"],
                                     record["delivery_id"])
                    self.assertEqual(state_path.read_bytes(), before_fleet)
                    peer_steward._resume_registered_obligation(duty, store)
                    transferred = (store.get(duty["id"]).get("observation") or {}).get("transfer_ref")
                    self.assertTrue(transferred)
                    exact = pending.read(self.jobs.parent, self.parent, record["delivery_id"])
                    self.assertEqual(exact["state"], "acked")
                    self.assertEqual(exact["receipt_digest"], record["receipt_digest"])
                    self.assertEqual((exact.get("receipt") or {}).get("reason"), "attempt-stalled")
                    body = supervision.render_text(record["receipt"])
                    self.assertIn("no exact runtime/command process", body)
                    self.assertIn("dispatch-registry.py reconcile --jobs", body)
                    peer_steward._resume_registered_obligation(store.get(duty["id"]), store)
                    self.assertEqual(store.get(duty["id"])["state"], "complete")

                    # ACK proves the receiver consumed this advisory, not that the
                    # attempt recovered. A fresh observation of progress clears it.
                    self.assertTrue(supervision.notice_is_current(record))
                    with mock.patch.object(dispatch_contract, "attempt_execution_observation",
                            return_value=dispatch_contract.AttemptExecutionObservation(
                                "active", "runtime-command-live")), \
                         mock.patch.object(supervision, "_attempt_human_wait", return_value=False):
                        current = supervision.update_attempt_stall_observation(
                            self.jobs, self.attempt, now=200.0, row=("open", metadata),
                            worktree=str(self.root))
                        self.assertEqual(current["state"], "active")
                        self.assertFalse(supervision.notice_is_current(record))
                        self.assertIsNone(_attempt_stall_attention(fleet_job))
                    with mock.patch.object(dispatch_contract, "attempt_execution_observation",
                            return_value=dispatch_contract.AttemptExecutionObservation(
                                "absent", "carrier-only")), \
                         mock.patch.object(supervision, "_attempt_human_wait", return_value=False), \
                         mock.patch("peer_obligations.ensure_normal_runner"):
                        new_episode = supervision.update_attempt_stall_observation(
                            self.jobs, self.attempt, now=300.0, row=("open", metadata),
                            worktree=str(self.root))
                        next_notice = supervision.materialize(
                            self.jobs, {self.attempt}, reason="attempt-stalled")[0]
                    self.assertEqual(new_episode["state"], "stalled")
                    self.assertNotEqual(new_episode["episode_id"], initial_episode)
                    self.assertNotEqual(next_notice["delivery_id"], record["delivery_id"])
                    self.assertEqual(self.jobs.read_text(encoding="utf-8").split("\t")[1], "open")

    def test_a_notice_observes_its_attempt_once_per_collection_pass(self):
        """notice_is_current asks about the stall twice (revision, then pending). Inside a Fleet pass the
        first observation is the pass's; outside one, and in the next pass, each question observes."""
        from fleet import pass_scope
        _metadata, _carrier, record, _duty = self._materialize_stall("claude")
        env = {"XDG_STATE_HOME": str(self.root / "xdg-state"),
               "AGENT_PEER_LEDGER_ROOT": str(self.root / "peer-state")}
        real = supervision.update_attempt_stall_observation
        with mock.patch.dict(os.environ, env), mock.patch.object(supervision, "_attempt_human_wait", return_value=False):
            with mock.patch.object(supervision, "update_attempt_stall_observation", wraps=real) as observe:
                supervision.notice_is_current(record)
                outside = observe.call_count
            with mock.patch.object(supervision, "update_attempt_stall_observation", wraps=real) as observe:
                with pass_scope.collection_pass():
                    supervision.notice_is_current(record)
                    first_pass = observe.call_count
                with pass_scope.collection_pass():
                    supervision.notice_is_current(record)
                    both_passes = observe.call_count
        self.assertEqual(outside, 2)                 # no pass: every question observes
        self.assertEqual(first_pass, 1)              # in a pass one observation serves both questions
        self.assertEqual(both_passes, 2)             # the next pass observes again

    def test_a_carried_row_across_two_passes_sees_the_stall_threshold_the_clock_crossed(self):
        """A warm process carries one row object from pass to pass. What the first pass observed
        (a clock before the stall threshold) is not what the second pass sees once the clock has
        crossed it: the second pass observes again, from its own clock, exactly as a fresh call does."""
        from fleet import pass_scope
        _metadata, _carrier, record, _duty = self._materialize_stall("claude")
        env = {"XDG_STATE_HOME": str(self.root / "xdg-state"),
               "AGENT_PEER_LEDGER_ROOT": str(self.root / "peer-state")}
        receipt = record["receipt"]
        window = 300.0                      # update_attempt_stall_observation's progress window
        before_threshold, past_threshold = 100.0 + 10.0, 100.0 + 2 * window + 100.0
        with mock.patch.dict(os.environ, env), mock.patch.object(supervision, "_attempt_human_wait", return_value=False):
            rows = supervision._rows(Path(receipt["job_registry"]),
                                     set(receipt["monitored_attempt_ids"]) | {receipt["owner_attempt_id"]})
            carried = rows[self.attempt]    # the one row object both passes are handed
            seen = []
            real = supervision.update_attempt_stall_observation

            def observe(*args, **kwargs):
                seen.append(real(*args, **kwargs))
                return seen[-1]

            with mock.patch.object(supervision, "_rows", return_value=rows), \
                 mock.patch.object(supervision, "update_attempt_stall_observation", side_effect=observe):
                with mock.patch.object(supervision.time, "time", return_value=before_threshold):
                    with pass_scope.collection_pass():
                        first = supervision._stall_observation_for_notice(self.jobs, self.attempt, carried)
                        self.assertTrue(supervision.notice_is_current(record))
                with mock.patch.object(supervision.time, "time", return_value=past_threshold):
                    with pass_scope.collection_pass():
                        second = supervision._stall_observation_for_notice(self.jobs, self.attempt, carried)
                        still_current = supervision.notice_is_current(record)
            fresh = real(self.jobs, self.attempt, now=past_threshold, persist=False, row=carried)
        self.assertEqual(first["observed_at"], before_threshold)
        self.assertEqual(first["quiet_windows"], 0)
        self.assertEqual(fresh["quiet_windows"], 2)               # the threshold the clock crossed
        self.assertEqual(second, fresh)                           # computed again, from the second clock
        self.assertEqual(second["observed_at"], past_threshold)
        self.assertEqual(second["quiet_windows"], 2)
        self.assertTrue(still_current)
        self.assertEqual(seen[-1]["observed_at"], past_threshold)   # notice_is_current's own question too

    def test_normal_observer_reentry_discovers_sidecarless_open_attempt_once(self):
        metadata, carrier = self._row("codex")
        before = self.jobs.read_bytes()
        execution = dispatch_contract.AttemptExecutionObservation("absent", "carrier-only")
        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(self.root / "xdg-state"),
                                          "AGENT_PEER_LEDGER_ROOT": str(self.root / "peer-state")}), \
             mock.patch.object(dispatch_contract, "attempt_execution_observation",
                return_value=execution), \
             mock.patch.object(supervision, "_attempt_human_wait", return_value=False), \
             mock.patch("peer_obligations.ensure_normal_runner"):
            first = supervision.update_open_attempt_stall_observations(self.jobs)
            second = supervision.update_open_attempt_stall_observations(self.jobs)
            signature = supervision.stall_observer_signature(self.jobs)
        self.assertEqual(len(first), 1)
        self.assertEqual([item["delivery_id"] for item in first],
                         [item["delivery_id"] for item in second])
        self.assertEqual(len(signature), 1)
        self.assertEqual(signature[0][0], self.attempt)
        self.assertEqual(self.jobs.read_bytes(), before)
        duties = peer_obligations.ObligationStore(self.jobs.parent).list(
            states={"pending"}, kinds={"registered-batch"})
        self.assertEqual(len(duties), 1)
        self.assertEqual(duties[0]["intent"]["carrier"], carrier)
        with mock.patch.object(supervision, "_attempt_human_wait", return_value=False):
            self.assertTrue(supervision.has_unowned_stall_observation(self.jobs))

    def test_recovery_after_claim_is_rechecked_before_any_parent_send(self):
        import dispatch_pending_delivery as queue
        metadata, _carrier, record, duty = self._materialize_stall("codex")
        store = peer_obligations.ObligationStore(self.jobs.parent)
        current = {"execution": dispatch_contract.AttemptExecutionObservation(
            "absent", "carrier-only")}
        census = {"result": {"agents": [{"agent": "codex", "pane_id": "w1:p5",
            "agent_session": {"value": self.parent}}]}}
        original_claim = queue.claim_ready_batch
        sends = []

        def claim_then_recover(*args, **kwargs):
            claimed = original_claim(*args, **kwargs)
            current["execution"] = dispatch_contract.AttemptExecutionObservation(
                "active", "runtime-command-live")
            supervision.update_attempt_stall_observation(
                self.jobs, self.attempt, now=200.0, row=("open", metadata),
                worktree=str(self.root))
            return claimed

        with mock.patch.dict(os.environ, {"AGENT_PEER_LEDGER_ROOT": str(self.root / "peer-state"),
                                          "XDG_STATE_HOME": str(self.root / "xdg-state")}), \
             mock.patch.object(dispatch_contract, "attempt_execution_observation",
                               side_effect=lambda _meta: current["execution"]), \
             mock.patch.object(supervision, "_attempt_human_wait", return_value=False), \
             mock.patch.object(peer_steward.subprocess, "run",
                return_value=SimpleNamespace(stdout=json.dumps(census), returncode=0)), \
             mock.patch.object(peer_steward, "_resolve_target", return_value=("codex", self.parent)), \
             mock.patch.object(queue, "claim_ready_batch", side_effect=claim_then_recover), \
             mock.patch.object(peer_steward, "cmd_prompt", side_effect=lambda *a, **k:
                sends.append((a, k)) or 0), \
             mock.patch.object(peer_steward.peer_message, "bind_dispatch_notice"), \
             mock.patch.object(peer_steward.peer_message, "prepare_peer_message",
                side_effect=AssertionError("stale notice must not prepare a transfer")):
            peer_steward._resume_registered_obligation(duty, store)
            peer_steward._resume_registered_obligation(store.get(duty["id"]), store)

        self.assertEqual(sends, [])
        latest = pending.read(self.jobs.parent, self.parent, record["delivery_id"])
        self.assertEqual(latest["state"], "rejected")
        self.assertEqual(store.get(duty["id"])["state"], "complete")


if __name__ == "__main__":
    unittest.main()
