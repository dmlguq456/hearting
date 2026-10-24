#!/usr/bin/env python3
"""Temporary-root checks for durable peer obligations and native readiness."""

from __future__ import annotations

import fcntl
import importlib.util
import fcntl
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import peer_obligations as obligations
import dispatch_batch_obligations as batch_obligations

_HERE = Path(__file__).resolve().parent
_STEWARD_SPEC = importlib.util.spec_from_file_location(
    "peer_steward_under_obligation_test", str(_HERE / "peer-steward.py"))
peer_steward = importlib.util.module_from_spec(_STEWARD_SPEC)
_STEWARD_SPEC.loader.exec_module(peer_steward)


class ObligationStoreTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.jobs = self.root / "route-jobs.tsv"
        self.jobs.touch()
        self._saved_env = dict(os.environ)
        os.environ.update({
            "HOME": str(self.root / "home"),
            "XDG_STATE_HOME": str(self.root / "xdg-state"),
            "XDG_CONFIG_HOME": str(self.root / "xdg-config"),
            "HARNESS_STATE_ROOT": str(self.root / "harness-state"),
            "AGENT_PEER_LEDGER_ROOT": str(self.root / "peer-ledger"),
            "AGENT_DISPATCH_JOBS": str(self.jobs),
            "CODEX_HOME": str(self.root / "home" / ".codex"),
            "CLAUDE_CONFIG_DIR": str(self.root / "home" / ".claude"),
        })
        self.store = obligations.ObligationStore(self.root)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._saved_env)
        self.temp.cleanup()

    @staticmethod
    def _exact_received(ref, digest, recipient, received_at=123.0):
        binding = {"recipient": dict(recipient), "watches": []}
        return {"ref": ref, "body_sha256": digest, "state": "received",
                "to": dict(recipient), "refs": [], "receipt": "exact-peer-ref",
                "received_at": received_at,
                "exact_receiver_receipt": {
                    "receipt_type": "exact-peer-ref",
                    "provenance": "validated-peer-ref-receiver-v1",
                    "transfer_ref": ref, "transfer_body_sha256": digest,
                    "effective_recipient": dict(recipient),
                    "recipient_binding": binding, "received_at": received_at}}

    def test_list_reuses_json_generation_and_observes_external_changes(self):
        first = self.store.create("cached-one", "message", {}, {"ref": "a" * 32})
        second = self.store.create("cached-two", "retire", {}, {"target": "pane"})
        self.store.reset_discovery_metrics()

        listed = self.store.list(states={"pending"})
        self.assertEqual({row["id"] for row in listed}, {first["id"], second["id"]})
        listed[0]["intent"]["mutated"] = True
        self.assertEqual(len(self.store.list(states={"pending"})), 2)
        metrics = self.store.discovery_metrics()
        self.assertEqual(metrics["json_reads"], 2)
        self.assertEqual(metrics["json_parses"], 2)
        self.assertGreater(metrics["stat_calls"], 2)

        path = self.store._record_path(first["id"])
        old_mtime = path.stat().st_mtime_ns
        raw = path.read_text(encoding="utf-8")
        self.assertIn('"state":"pending"', raw)
        changed = raw.replace('"state":"pending"', '"state":"unknown"')
        self.assertEqual(len(raw), len(changed))
        path.write_text(changed, encoding="utf-8")
        os.utime(path, ns=(old_mtime, old_mtime))
        self.assertNotIn(first["id"], {row["id"] for row in self.store.list(states={"pending"})})
        self.assertIn(first["id"], {row["id"] for row in self.store.list(states={"unknown"})})
        self.assertNotIn("mutated", self.store.get(first["id"])["intent"])

        peer = obligations.ObligationStore(self.root)
        peer.create("cached-three", "message", {}, {"ref": "c" * 32})
        self.assertIn("cached-three", {row["id"] for row in self.store.list(states={"pending"})})
        self.store._record_path(second["id"]).unlink()
        self.assertNotIn(second["id"], {row["id"] for row in self.store.list(states={"pending"})})

        refreshed = self.store.get(first["id"])
        self.store.update(first["id"], state="complete")
        self.assertEqual(self.store.list(states={"complete"})[0]["state"], "complete")
        self.assertEqual(refreshed["state"], "unknown")

    def test_terminal_files_are_not_restatted_after_unrelated_directory_changes(self):
        first = self.store.create("terminal-cache-one", "readiness-observation", {}, {})
        second = self.store.create("terminal-cache-two", "readiness-observation", {}, {})
        self.store.update(first["id"], state="complete", result="consumer-exited")
        self.store.update(second["id"], state="cancelled", result="consumer-exited")
        terminal_at = self.store.get(first["id"])["terminal_at"]
        self.assertEqual(self.store.list(), [])
        self.store.reset_discovery_metrics()
        self.assertEqual(self.store.list(), [])
        metrics = self.store.discovery_metrics()
        self.assertEqual(metrics["stat_calls"], 0)
        self.assertEqual(metrics["active_full_scans"], 0)

        self.store.create("unrelated-active", "message", {}, {})
        self.assertEqual(len(self.store.list()), 1)
        metrics = self.store.discovery_metrics()
        self.assertEqual(metrics["active_index_entries"], 1)
        self.assertEqual(metrics["stat_calls"], 2,
                         "only the active row should be statted and read")
        refreshed = self.store.update(first["id"], observation={"late": "metadata"})
        self.assertEqual(refreshed["terminal_at"], terminal_at)

    def test_recovery_does_not_recreate_a_removed_state_root(self):
        state_root = self.root / "removed-state"
        state_root.mkdir()
        store = obligations.ObligationStore(state_root)
        store.create("before-removal", "message", {}, {})
        moved = state_root.with_name("removed-state-held")
        state_root.rename(moved)

        self.assertFalse(store.ensure_directory())
        with self.assertRaises(obligations.ObligationError):
            store.create("after-removal", "message", {}, {})
        self.assertFalse(state_root.exists())

    def test_terminal_retention_is_due_bounded_and_preserves_live_work(self):
        now = time.time()
        old_ids = []
        for index in range(20):
            duty_id = f"old-readiness-{index:02d}"
            duty = self.store.create(duty_id, "readiness-observation",
                {"consumer": {"pid": 2_000_000_000 + index,
                              "pid_start": "1"}}, {})
            terminal = self.store.update(duty["id"], state="complete",
                                         result="consumer-exited")
            record_path = self.store._record_path(duty["id"])
            persisted = json.loads(record_path.read_text(encoding="utf-8"))
            persisted["terminal_at"] = now - 2 * 86400
            record_path.write_text(json.dumps(persisted), encoding="utf-8")
            self.assertEqual(terminal["state"], "complete")
            old_ids.append(duty_id)

        pending = self.store.create("old-active", "message", {}, {})
        unknown = self.store.create("old-unknown", "registered-batch", {}, {})
        self.store.update(unknown["id"], state="unknown", observer_error="unavailable")
        result = self.store.prune_terminal_obligations(now=now)
        self.assertLessEqual(result["examined"], 64)
        self.assertEqual(result["removed"], 16)
        self.assertEqual(sum(self.store.get(duty_id) is None for duty_id in old_ids), 16)
        self.assertEqual(self.store.prune_terminal_obligations(now=now)["status"], "not-due")

        next_pass = self.store.prune_terminal_obligations(now=now + 300)
        self.assertEqual(next_pass["removed"], 4)
        self.assertIsNotNone(self.store.get(pending["id"]))
        self.assertEqual(self.store.get(unknown["id"])["state"], "unknown")

    def test_pruned_registered_delivery_source_does_not_reaccept_a_duty(self):
        from types import SimpleNamespace
        import dispatch_pending_delivery as pending
        from harness_capabilities import parent_completion

        jobs = self.root / "dispatch" / "jobs.log"
        jobs.parent.mkdir()
        jobs.touch()
        attempt, storage, delivery_id = "att-pruned", "parent-storage", "delivery-pruned"
        carrier = parent_completion("codex")["carrier"]
        digest, revision = "d" * 64, "row-revision"
        metadata = {
            "attempt_id": attempt, "parent_completion_delivery": carrier,
            "parent_sid": storage, "delivery_intent": "1", "delivery_id": delivery_id,
            "delivery_receipt_digest": digest, "delivery_row_revision": revision,
        }
        row = SimpleNamespace(status="done", metadata=metadata, attempt_id=attempt)
        terminal = {"delivery_id": delivery_id, "recipient_kind": carrier,
                    "attempt_ids": [attempt], "state": "acked",
                    "receipt_digest": digest, "row_revisions": {attempt: revision}}
        with mock.patch("dispatch_completion_join.current_attempt_row", return_value=row), \
             mock.patch("dispatch_seat_handover.effective_parent", return_value="sid-parent"), \
             mock.patch("dispatch_pending_delivery.read", return_value=terminal), \
             mock.patch("dispatch_pending_delivery.received_ack_identity", return_value={}), \
             mock.patch.object(obligations, "ensure_normal_runner") as ensure, \
             mock.patch.object(obligations.ObligationStore, "create", autospec=True) as create:
            self.assertTrue(obligations.retain_registered_completion(
                jobs, attempt, "sid-parent", carrier))
        create.assert_not_called()
        ensure.assert_not_called()

        tomb = pending.tombstone_path(pending.record_path(jobs.parent, storage, delivery_id))
        tomb.parent.mkdir(parents=True, exist_ok=True)
        tomb.write_text(json.dumps({
            "schema": pending.PRUNE_TOMBSTONE_SCHEMA,
            "delivery_id": delivery_id,
            "recipient_digest": "wrong-recipient",
            "attempt_ids": [attempt],
            "parent_attempt_id": "att-parent",
            "terminal_state": "expired",
        }), encoding="utf-8")
        with mock.patch("dispatch_completion_join.current_attempt_row", return_value=row), \
             mock.patch("dispatch_seat_handover.effective_parent", return_value="sid-parent"), \
             mock.patch("dispatch_pending_delivery.read", return_value=None), \
             mock.patch.object(obligations, "ensure_normal_runner") as ensure, \
             mock.patch.object(obligations.ObligationStore, "create", autospec=True) as create:
            self.assertTrue(obligations.retain_registered_completion(
                jobs, attempt, "sid-parent", carrier))
        create.assert_called_once()
        ensure.assert_called_once()

        tomb.write_text(json.dumps({
            "schema": pending.PRUNE_TOMBSTONE_SCHEMA,
            "delivery_id": delivery_id,
            "recipient_digest": pending.recipient_digest(storage),
            "attempt_ids": [attempt],
            "parent_attempt_id": "att-parent",
            "terminal_state": "expired",
        }), encoding="utf-8")
        with mock.patch("dispatch_completion_join.current_attempt_row", return_value=row), \
             mock.patch("dispatch_seat_handover.effective_parent", return_value="sid-parent"), \
             mock.patch("dispatch_pending_delivery.read", return_value=None), \
             mock.patch.object(obligations, "ensure_normal_runner") as ensure, \
             mock.patch.object(obligations.ObligationStore, "create", autospec=True) as create:
            self.assertTrue(obligations.retain_registered_completion(
                jobs, attempt, "sid-parent", carrier))
        create.assert_not_called()
        ensure.assert_not_called()

    def test_runner_pass_shares_discovery_but_next_pass_observes_external_rewrite(self):
        duty = self.store.create("pass-snapshot", "retire", {}, {"target": "pane"})
        self.store.begin_read_pass()
        self.assertEqual(self.store.list(states={"pending"}, kinds={"retire"})[0]["id"], duty["id"])
        path = self.store._record_path(duty["id"])
        raw = path.read_text(encoding="utf-8")
        path.write_text(raw.replace('"state":"pending"', '"state":"unknown"'), encoding="utf-8")
        self.assertEqual(self.store.list(states={"pending"}, kinds={"retire"})[0]["id"], duty["id"])
        self.store.end_read_pass()
        self.assertEqual(self.store.list(states={"pending"}, kinds={"retire"}), [])
        self.assertEqual(self.store.list(states={"unknown"}, kinds={"retire"})[0]["state"], "unknown")

    def test_skip_unchanged_preserves_default_revision_updates(self):
        duty = self.store.create("message-noop", "message", {}, {})
        observation = {"ref": "r" * 32, "reason": "target-identity-changed"}
        updated = self.store.update(duty["id"], state="unknown", observation=observation)

        same = self.store.update(duty["id"], state="unknown", observation=observation,
                                 skip_unchanged=True)
        self.assertEqual(same, updated)

        default_update = self.store.update(duty["id"], state="unknown", observation=observation)
        self.assertEqual(default_update["revision"], updated["revision"] + 1)
        self.assertGreaterEqual(default_update["updated_at"], updated["updated_at"])

    def test_completed_retire_remains_active_only_while_its_followup_is_pending(self):
        duty = self.store.create("retire-followup", "retire", {"pane": "w1:p1"},
                                 {"target": "fixture"})
        self.store.update(duty["id"], state="complete", result="target-already-gone",
                          cleanup="complete", observation={"phase": "complete"})
        self.assertEqual([row["id"] for row in self.store.list()], [duty["id"]])
        self.store.update(duty["id"], observation={"phase": "complete",
                          "terminal_followup": "complete"})
        self.assertEqual(self.store.list(), [])

    def test_message_receipt_requires_matching_ref_recipient_and_body_digest(self):
        ref, digest = "a" * 32, "b" * 64
        recipient = {"harness": "codex", "session_id": "sid-recipient"}
        identity = {"server": "default", "pane": "", **recipient}
        duty = self.store.create("message-" + ref, "message", identity,
                                 {"ref": ref, "body_digest": digest,
                                  "to": dict(recipient)})
        received = self._exact_received(ref, digest, recipient)
        generic = {key: value for key, value in received.items()
                   if key != "exact_receiver_receipt"}
        self.assertFalse(self.store.acknowledge_message_receipt(
            duty["id"], generic, recipient))

        self.assertFalse(self.store.acknowledge_message_receipt(
            duty["id"], {**received, "ref": "c" * 32}, recipient))
        self.assertFalse(self.store.acknowledge_message_receipt(
            duty["id"], {**received, "body_sha256": "d" * 64}, recipient))
        self.assertFalse(self.store.acknowledge_message_receipt(
            duty["id"], received, {"harness": "codex", "session_id": "foreign"}))
        self.assertEqual(self.store.get(duty["id"])["state"], "pending")

        self.assertTrue(self.store.acknowledge_message_receipt(duty["id"], received, recipient))
        settled = self.store.get(duty["id"])
        self.assertEqual((settled["state"], settled["result"], settled["delivery"]),
                         ("complete", "received", "acknowledged"))

    def test_cancelled_message_keeps_late_exact_receipt_without_reopening(self):
        ref, digest = "e" * 32, "f" * 64
        recipient = {"harness": "claude", "session_id": "sid-late", "name": "late"}
        identity = {"server": "default", "pane": "w1:p1", **recipient}
        duty = self.store.create("message-" + ref, "message", identity,
                                 {"ref": ref, "body_digest": digest,
                                  "to": dict(recipient)})
        pending = {"ref": ref, "body_sha256": digest, "state": "queued",
                   "to": dict(recipient)}
        evidence = {"kind": "recipient-session-absent", "server": "default",
                    "harness": "claude", "session_id": "sid-late",
                    "census_complete": True, "session_absent": True,
                    "census_endpoint": "default"}
        cancelled = self.store.cancel_message_delivery(
            duty["id"], expected_revision=duty["revision"], ref=ref,
            body_digest=digest, identity=identity, message=pending, evidence=evidence)
        self.assertEqual(cancelled["state"], "cancelled")
        self.assertIsNone(cancelled["result"])
        self.assertEqual(cancelled["delivery"], "failed")

        stale = self.store.update_message_checkpoint(
            duty["id"], expected_revision=duty["revision"], state="unknown",
            observation={"ref": ref, "reason": "stale-runner-checkpoint"})
        self.assertEqual(stale["state"], "cancelled")

        received = self._exact_received(ref, digest, recipient, received_at=456.0)
        self.assertTrue(self.store.acknowledge_message_receipt(duty["id"], received, recipient))
        late = self.store.get(duty["id"])
        self.assertEqual((late["state"], late["result"], late["delivery"]),
                         ("cancelled", None, "acknowledged"))
        self.assertEqual(late["observation"]["reason"], "recipient-session-absent")
        self.assertIn("terminal_evidence", late["observation"])
        self.assertEqual(late["observation"]["receiver_receipt"]["ref"], ref)

    def test_receipt_wins_over_a_stale_terminal_cancellation(self):
        ref, digest = "1" * 32, "2" * 64
        recipient = {"harness": "codex", "session_id": "sid-race"}
        identity = {"server": "default", "pane": "", **recipient}
        duty = self.store.create("message-" + ref, "message", identity,
                                 {"ref": ref, "body_digest": digest,
                                  "to": dict(recipient)})
        received = self._exact_received(ref, digest, recipient, received_at=789.0)
        self.assertTrue(self.store.acknowledge_message_receipt(duty["id"], received, recipient))

        stale = {"ref": ref, "body_sha256": digest, "state": "queued", "to": dict(recipient)}
        evidence = {"kind": "recipient-session-absent", "server": "default",
                    "harness": "codex", "session_id": "sid-race",
                    "census_complete": True, "session_absent": True,
                    "census_endpoint": "default"}
        result = self.store.cancel_message_delivery(
            duty["id"], expected_revision=duty["revision"], ref=ref,
            body_digest=digest, identity=identity, message=stale, evidence=evidence)
        self.assertEqual((result["state"], result["result"], result["delivery"]),
                         ("complete", "received", "acknowledged"))

    def test_exact_ack_and_terminal_cancel_race_preserves_receipt_without_reopening(self):
        ref, digest = "3" * 32, "4" * 64
        recipient = {"harness": "codex", "session_id": "sid-race-both"}
        identity = {"server": "default", "pane": "", **recipient}
        duty = self.store.create("message-" + ref, "message", identity,
                                 {"ref": ref, "body_digest": digest,
                                  "to": dict(recipient), "refs": []})
        received = self._exact_received(ref, digest, recipient, received_at=987.0)
        pending = {"ref": ref, "body_sha256": digest, "state": "queued",
                   "to": dict(recipient), "refs": []}
        evidence = {"kind": "recipient-session-absent", "server": "default",
                    "harness": recipient["harness"], "session_id": recipient["session_id"],
                    "census_complete": True, "session_absent": True,
                    "census_endpoint": "default"}
        barrier = threading.Barrier(3)
        outcomes = []

        def acknowledge():
            barrier.wait(timeout=3)
            outcomes.append(self.store.acknowledge_message_receipt(
                duty["id"], received, recipient))

        def cancel():
            barrier.wait(timeout=3)
            outcomes.append(self.store.cancel_message_delivery(
                duty["id"], expected_revision=duty["revision"], ref=ref,
                body_digest=digest, identity=identity, message=pending, evidence=evidence))

        threads = [threading.Thread(target=acknowledge), threading.Thread(target=cancel)]
        for thread in threads:
            thread.start()
        barrier.wait(timeout=3)
        for thread in threads:
            thread.join(timeout=5)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(len(outcomes), 2)
        latest = self.store.get(duty["id"])
        self.assertIn(latest["state"], {"complete", "cancelled"})
        self.assertEqual(latest["delivery"], "acknowledged")
        self.assertEqual(latest["result"], "received" if latest["state"] == "complete" else None)
        self.assertEqual(latest["observation"]["receiver_receipt"]["ref"], ref)

    def test_inherited_batch_lease_requires_flock_and_exact_observer_row(self):
        from types import SimpleNamespace

        jobs = self.jobs
        jobs.parent.mkdir(parents=True, exist_ok=True)
        jobs.touch(exist_ok=True)
        args = SimpleNamespace(
            jobs=jobs, sealed_batch_id="sealed-lease-fixture",
            parent_attempt_id="att-lease-parent", parent_session_id=None,
            attempt_id=["att-lease-child"], control_socket=None, queue_socket=None,
            thread_id=None, interval=1.0, timeout=30.0,
        )
        duty_id, _record = batch_obligations.create(args)
        lease_path = batch_obligations._root(jobs) / (duty_id + ".lease")
        lease_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(lease_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            self.assertIsNone(batch_obligations.acquire(jobs, duty_id, fd))
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(batch_obligations.acquire(jobs, duty_id, fd), fd)
            claimed = batch_obligations.update_for_observer(
                jobs, duty_id, fd, claim=True, reason="observing")
            self.assertIsNotNone(claimed)
            self.assertTrue(batch_obligations.observer_owns_current(jobs, duty_id, fd))

            batch_obligations.update(jobs, duty_id, state="unknown",
                                     observer={"pid": 1, "pid_start": "1"})
            before = batch_obligations.read(jobs, duty_id)
            self.assertFalse(batch_obligations.observer_owns_current(jobs, duty_id, fd))
            self.assertIsNone(batch_obligations.update_for_observer(
                jobs, duty_id, fd, claim=True,
                expected_observer={"pid": os.getpid(),
                                   "pid_start": batch_obligations._pid_start(os.getpid())},
                reason="replacement-race"))
            self.assertIsNone(batch_obligations.update_for_observer(
                jobs, duty_id, fd, state="complete", reason="stale-observer"))
            self.assertEqual(batch_obligations.read(jobs, duty_id), before)
        finally:
            batch_obligations.release(fd, lease_path)

        reused = os.open(jobs, os.O_RDONLY)
        try:
            self.assertIsNone(batch_obligations.acquire(jobs, duty_id, reused))
            batch_obligations.release(reused, lease_path)
            os.fstat(reused)
        finally:
            os.close(reused)

    def test_post_spawn_record_failure_keeps_child_lease_locked(self):
        from types import SimpleNamespace

        jobs = self.jobs
        jobs.parent.mkdir(parents=True, exist_ok=True)
        jobs.touch(exist_ok=True)
        args = SimpleNamespace(
            jobs=jobs, sealed_batch_id="sealed-spawn-failure",
            parent_attempt_id="att-spawn-parent", parent_session_id=None,
            attempt_id=["att-spawn-child"], control_socket=None, queue_socket=None,
            thread_id=None, interval=1.0, timeout=30.0,
        )
        duty_id, _record = batch_obligations.create(args)
        lease_path = batch_obligations._root(jobs) / (duty_id + ".lease")
        real_popen = subprocess.Popen
        children = []

        def launch_child(_command, **kwargs):
            child = real_popen([sys.executable, "-c", "import time; time.sleep(30)"], **kwargs)
            children.append(child)
            return child

        try:
            with mock.patch.object(obligations, "service_script",
                                   return_value=(Path("/fixture/sidecar.py"), {}, None)), \
                 mock.patch.object(batch_obligations, "_jobs_paths", return_value=[jobs]), \
                 mock.patch.object(batch_obligations, "_resume_owner_settlements",
                                   return_value=[]), \
                 mock.patch.object(batch_obligations, "_write",
                                   side_effect=batch_obligations.BatchObligationError(
                                       "simulated-record-update-failure")), \
                 mock.patch.object(batch_obligations.subprocess, "Popen",
                                   side_effect=launch_child):
                self.assertEqual(batch_obligations.ensure_observers(jobs), 0)

            self.assertEqual(len(children), 1)
            probe = os.open(lease_path, os.O_RDWR)
            try:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                os.close(probe)

            children[0].terminate()
            children[0].wait(timeout=5)
            probe = os.open(lease_path, os.O_RDWR)
            try:
                fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(probe, fcntl.LOCK_UN)
            finally:
                os.close(probe)
        finally:
            for child in children:
                if child.poll() is None:
                    child.kill()
                    child.wait(timeout=5)

    def test_retry_handoff_keeps_the_lease_with_the_successor(self):
        from types import SimpleNamespace

        jobs = self.jobs
        jobs.parent.mkdir(parents=True, exist_ok=True)
        jobs.touch(exist_ok=True)
        args = SimpleNamespace(
            jobs=jobs, sealed_batch_id="sealed-handoff",
            parent_attempt_id="att-handoff-parent", parent_session_id=None,
            attempt_id=["att-handoff-child"], control_socket=None, queue_socket=None,
            thread_id=None, interval=1.0, timeout=30.0,
        )
        duty_id, _record = batch_obligations.create(args)
        lease_path = batch_obligations._root(jobs) / (duty_id + ".lease")
        fd = batch_obligations.acquire(jobs, duty_id)
        self.assertIsNotNone(fd)
        child = None
        children = []
        try:
            self.assertIsNotNone(batch_obligations.update_for_observer(
                jobs, duty_id, fd, claim=True, reason="observing"))
            real_popen = subprocess.Popen

            def launch_child(_command, **kwargs):
                probe = os.open(batch_obligations._record_path(jobs, duty_id).with_suffix(".update.lock"), os.O_RDWR)
                try:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
                finally:
                    os.close(probe)
                process = real_popen(
                    [sys.executable, "-c", "import time; time.sleep(30)"], **kwargs)
                children.append(process)
                return process

            with mock.patch.object(obligations, "service_script",
                                   return_value=(Path("/fixture/sidecar.py"), {}, None)), \
                 mock.patch.object(batch_obligations.subprocess, "Popen",
                                   side_effect=launch_child):
                self.assertTrue(batch_obligations.handoff_observer(
                    jobs, duty_id, fd))
            child = children[0]
            owner = batch_obligations.read(jobs, duty_id)["observer"]
            self.assertEqual(owner["pid"], child.pid)
            self.assertTrue(owner["pid_start"].isdecimal())
            self.assertTrue(batch_obligations._fd_holds_lease(fd, lease_path))

            os.close(fd)
            fd = None
            probe = os.open(lease_path, os.O_RDWR)
            try:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                os.close(probe)

            child.terminate()
            child.wait(timeout=5)
            probe = os.open(lease_path, os.O_RDWR)
            try:
                fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(probe, fcntl.LOCK_UN)
            finally:
                os.close(probe)
        finally:
            if fd is not None:
                batch_obligations.release(fd, lease_path)
            if child is not None and child.poll() is None:
                child.kill()
                child.wait(timeout=5)

    def test_current_runner_starts_once_while_legacy_runner_lock_is_held(self):
        self.store.create("message-reconnect", "message", {"session_id": "parent"},
                          {"ref": "a" * 32})
        with (self.store.root / "runner.lock").open("w") as legacy, \
             mock.patch.object(obligations.subprocess, "Popen") as spawn:
            fcntl.flock(legacy, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertTrue(obligations.ensure_runner(self.root))
            spawn.assert_called_once()
            argv = spawn.call_args.args[0]
            self.assertEqual(argv[-2:], ["--state-root", str(self.root)])
            with (self.store.root / obligations.RUNNER_LOCK_NAME).open("w") as current:
                fcntl.flock(current, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.assertTrue(obligations.ensure_runner(self.root))
                spawn.assert_called_once()

    def test_retire_lock_waits_for_legacy_and_releases_after_processing_error(self):
        self.store.root.mkdir(parents=True)
        with (self.store.root / "runner.lock").open("w") as legacy:
            fcntl.flock(legacy, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with obligations.legacy_runner_lock(self.store) as acquired:
                self.assertFalse(acquired)
        with self.assertRaisesRegex(RuntimeError, "fixture"):
            with obligations.legacy_runner_lock(self.store) as acquired:
                self.assertTrue(acquired)
                raise RuntimeError("fixture")
        with obligations.legacy_runner_lock(self.store) as acquired:
            self.assertTrue(acquired)

    def test_create_is_idempotent_and_keeps_body_out_of_duty_record(self):
        intent = {"ref": "a" * 32, "body_digest": "b" * 64}
        identity = {"server": "fixture-server", "pane": "w1:p7", "session_id": "sid-1"}
        first = self.store.create("message-" + "a" * 32, "message", identity, intent)
        again = self.store.create("message-" + "a" * 32, "message", identity, intent)
        self.assertEqual(first["id"], again["id"])
        self.assertEqual(first["intent"], again["intent"])
        self.assertNotIn("text", first["intent"])

    def test_immutable_identity_conflict_is_refused(self):
        self.store.create("retire-1", "retire", {"pane": "w1:p1"}, {"target": "worker"})
        with self.assertRaisesRegex(obligations.ObligationError, "intent-conflict"):
            self.store.create("retire-1", "retire", {"pane": "w1:p2"}, {"target": "worker"})

    def test_result_is_immutable_across_later_transport_updates(self):
        self.store.create("batch-1", "registered-batch", {"parent": "att-parent"},
                          {"attempt_ids": ["att-child"]})
        self.store.update("batch-1", result="succeeded", delivery="pending")
        updated = self.store.update("batch-1", observation={"reason": "receiver-unavailable"},
                                    delivery="pending")
        self.assertEqual(updated["result"], "succeeded")
        with self.assertRaisesRegex(obligations.ObligationError, "result-conflict"):
            self.store.update("batch-1", result="failed")

    def test_ordinary_pane_without_registry_row_is_valid_native_evidence(self):
        from peer_read_context import PeerReadContext

        jobs = self.root / "dispatch" / "jobs.log"
        work, state = obligations.bound_work_for_pane(
            "w1:p4", "codex", "thread-1", jobs=jobs)
        self.assertEqual((work, state), ((), "observed"))
        shared_work, shared_state = obligations.bound_work_for_pane(
            "w1:p4", "codex", "thread-1", jobs=jobs,
            read_context=PeerReadContext(registry_append_only=True),
            registry_append_only=True)
        self.assertEqual((shared_work, shared_state), ((), "observed"),
                         "a shared read context preserves a freshly observed absence")
        ready = obligations.pane_readiness(
            server="fixture-server", pane="w1:p4", harness="codex",
            session_id="thread-1", pid_birth="pid:9@start:4",
            identity_verified=True, native_turn="idle",
            bound_work=work, bindings_state=state,
        )
        self.assertEqual((ready.state, ready.scope, ready.outcome),
                         ("ready", "native-turn", None))

    def test_a_received_gate_does_not_close_a_later_terminal_completion(self):
        from types import SimpleNamespace
        import dispatch_pending_delivery as pending
        identity = {"jobs": str(self.jobs), "attempt_id": "att-owner",
                    "session_id": "sid", "harness": "claude"}
        duty = self.store.create("registered-batch-fixture", "registered-batch", identity,
                                 {"carrier": "claude-parent-runtime"})
        owner = "retained-rewake:" + duty["id"]
        observation = {"transfer_ref": "a" * 32, "recipient_sid": "sid",
                       "delivery_id": "delivery-gate", "storage_recipient": "sid", "claim_owner": owner}
        self.store.update(duty["id"], observation=observation)
        def transfer(ref, delivery_id, attempt_id="att-owner"):
            return {"state": "received", "receipt": "exact-peer-ref", "ref": ref,
                   "refs": [duty["id"]],
                   "to": {"harness": "claude", "session_id": "sid"},
                   "dispatch_notice": {"duty_id": duty["id"], "jobs": identity["jobs"],
                       "attempt_id": attempt_id, "carrier": "claude-parent-runtime",
                       "recipient_sid": "sid", "storage_recipient": "sid",
                       "delivery_id": delivery_id, "claim_owner": owner}}
        row = SimpleNamespace(status="done", attempt_id="att-owner",
                              metadata={"parent_sid": "sid", "parent_harness": "claude",
                                        "parent_completion_delivery": "claude-parent-runtime",
                                        "delivery_intent": "1",
                                        "delivery_id": "delivery-final",
                                        "delivery_receipt_digest": "digest-final",
                                        "delivery_row_revision": "rev-final"})
        def record(delivery_id, state, digest, revision):
            return {"delivery_id": delivery_id, "recipient_digest": pending.recipient_digest("sid"),
                    "recipient_kind": "claude-parent-runtime", "state": state,
                    "attempt_ids": ["att-owner"], "receipt_digest": digest,
                    "row_revisions": {"att-owner": revision}, "receipt": {"kind": "completion"},
                    "session_generation": "", "session_generation_supported": "0",
                    "parent_attempt_id": "att-parent", "route_id": "rt-fixture",
                    "route_node": "report", "lineage": []}
        gate = record("delivery-gate", "claimed", "digest-gate", "rev-gate")
        final = record("delivery-final", "claimed", "digest-final", "rev-final")
        records = {"delivery-gate": gate, "delivery-final": final}
        def read_record(_root, _storage, delivery_id):
            return records.get(delivery_id)
        def acknowledge(_root, _storage, delivery_id, **_kwargs):
            records[delivery_id]["state"] = "acked"
            return records[delivery_id]
        message = transfer("a" * 32, "delivery-gate")
        with mock.patch("dispatch_completion_join.current_attempt_row", return_value=row), \
                mock.patch("dispatch_seat_handover.effective_parent", return_value="sid"), \
                mock.patch("dispatch_pending_delivery.read", side_effect=read_record), \
                mock.patch("dispatch_pending_delivery.received_ack_identity",
                           side_effect=lambda value, _storage, _did: {"delivery_id": value["delivery_id"]}), \
                mock.patch("dispatch_pending_delivery.ack_received", side_effect=acknowledge) as ack:
            self.assertTrue(obligations.acknowledge_registered_delivery(message, roots=[self.root]))
            ack.assert_called_once()
            self.assertEqual(self.store.get(duty["id"])["state"], "pending")
            self.store.update(duty["id"], observation={**observation, "transfer_ref": "b" * 32,
                                                      "delivery_id": "delivery-final"})
            self.assertTrue(obligations.registered_delivery_settled(message, roots=[self.root]))
            obligations.acknowledge_registered_delivery(message, roots=[self.root])
            self.assertEqual(self.store.get(duty["id"])["state"], "pending")
            ack.reset_mock()

            for invalid in (
                {**message, "ref": "z" * 32},
                {**message, "to": {"harness": "claude", "session_id": "other"}},
                {**message, "to": {"harness": "codex", "session_id": "sid"}},
                transfer("b" * 32, "delivery-gate", attempt_id="att-other"),
                transfer("b" * 32, "delivery-missing"),
                {**message, "dispatch_notice": {
                    **message["dispatch_notice"], "storage_recipient": "other"}},
            ):
                self.assertFalse(obligations.acknowledge_registered_delivery(invalid, roots=[self.root]))
            self.assertFalse(obligations.acknowledge_registered_delivery(
                message, roots=[self.root / "foreign-artifacts"]))
            self.assertEqual(ack.call_count, 0)
            with mock.patch("dispatch_seat_handover.effective_parent", return_value="successor"):
                self.assertFalse(obligations.acknowledge_registered_delivery(message, roots=[self.root]))
            self.assertEqual(ack.call_count, 0)

            successor_duty = self.store.create(
                "registered-batch-successor", "registered-batch", identity,
                {"carrier": "claude-parent-runtime"})
            successor_owner = "retained-rewake:" + successor_duty["id"]
            records["delivery-successor"] = record(
                "delivery-successor", "claimed", "digest-successor", "rev-successor")
            prior_recipient_transfer = {"state": "received", "receipt": "exact-native-history",
                "ref": "c" * 32, "refs": [successor_duty["id"]],
                "to": {"harness": "claude", "session_id": "successor-b"},
                "dispatch_notice": {"duty_id": successor_duty["id"], "jobs": identity["jobs"],
                    "attempt_id": "att-owner", "carrier": "claude-parent-runtime",
                    "recipient_sid": "successor-b", "storage_recipient": "sid",
                    "delivery_id": "delivery-successor", "claim_owner": successor_owner}}
            current_recipient_transfer = {**prior_recipient_transfer, "ref": "d" * 32,
                "to": {"harness": "claude", "session_id": "successor-c"},
                "dispatch_notice": {**prior_recipient_transfer["dispatch_notice"],
                                    "recipient_sid": "successor-c"}}
            with mock.patch("dispatch_seat_handover.effective_parent", return_value="successor-c"):
                self.assertFalse(obligations.acknowledge_registered_delivery(
                    prior_recipient_transfer, roots=[self.root]))
                self.assertTrue(obligations.acknowledge_registered_delivery(
                    current_recipient_transfer, roots=[self.root]))
            self.assertEqual(records["delivery-successor"]["state"], "acked")
            ack.reset_mock()

            final_transfer = transfer("b" * 32, "delivery-final")
            final["receipt_digest"] = "tampered-digest"
            self.assertFalse(obligations.acknowledge_registered_delivery(final_transfer, roots=[self.root]))
            self.assertEqual(ack.call_count, 0)
            final["receipt_digest"] = "digest-final"
            real_update = obligations.ObligationStore.update
            failed_update = {"remaining": 1}
            def update_once(store, duty_id, **kwargs):
                if failed_update["remaining"] and kwargs.get("state") == "complete":
                    failed_update["remaining"] -= 1
                    raise OSError("simulated duty persistence interruption")
                return real_update(store, duty_id, **kwargs)
            with mock.patch.object(obligations.ObligationStore, "update", autospec=True,
                                   side_effect=update_once):
                self.assertTrue(obligations.acknowledge_registered_delivery(final_transfer,
                                                                            roots=[self.root]))
            self.assertEqual(self.store.get(duty["id"])["state"], "pending")
            self.assertEqual(final["state"], "acked")
            self.assertTrue(obligations.acknowledge_registered_delivery(final_transfer, roots=[self.root]))
            self.assertEqual(ack.call_count, 1)
            self.assertEqual(self.store.get(duty["id"])["state"], "complete")

    def test_registered_completion_producer_uses_shared_normal_runner_helper(self):
        from types import SimpleNamespace
        carrier = "fixture-parent-runtime"
        jobs = self.root / "dispatch" / "jobs.log"
        jobs.parent.mkdir()
        row = SimpleNamespace(status="running", metadata={
            "parent_completion_delivery": carrier})
        with mock.patch("dispatch_completion_join.current_attempt_row", return_value=row), \
             mock.patch("dispatch_seat_handover.effective_parent", return_value="sid-parent"), \
             mock.patch("harness_capabilities.parent_completion",
                        return_value={"carrier": carrier}), \
             mock.patch.object(obligations, "ensure_normal_runner") as ensure:
            obligations.retain_registered_completion(
                jobs, "att-fixture", "sid-parent", carrier)
        duty = obligations.ObligationStore(jobs.parent).list()[0]
        self.assertEqual(duty["intent"]["identity"]["attempt_id"], "att-fixture")
        ensure.assert_called_once_with(jobs.parent)

    def test_registered_completion_retains_exact_native_turn_before_ack(self):
        from types import SimpleNamespace
        import dispatch_completion_join as join
        import dispatch_pending_delivery as pending
        attempt, sid, carrier = "att-native-retained", "native-thread", "codex-native-queue"
        receipt = {"schema_version": 2, "state": "ready", "parent_attempt_id": "parent-session-native",
            "job_registry": str(self.jobs), "children": [{"attempt_id": attempt, "status": "done",
                "readiness": "ready", "reason": "registry-closed", "required_action": "advance-completed",
                "harness": "codex"}]}
        revision, delivery_id = "revision-native", "delivery-native-retained"
        metadata = {"attempt_id": attempt, "attempt_schema_version": "2", "dispatch_depth": "1",
            "worker_type": "stage", "parent_sid": sid, "parent_completion_delivery": carrier,
            "delivery_intent": "1", "delivery_id": delivery_id,
            "delivery_receipt_digest": pending._canonical_receipt_digest(receipt),
            "delivery_row_revision": revision, "route_id": "rt-native", "route_node": "execute",
            "parent_attempt_id": "-", "harness": "codex"}
        row = SimpleNamespace(status="done", metadata=metadata)
        pending.create(self.root, recipient_kind=carrier, recipient_key=sid, delivery_id=delivery_id,
            session_generation="", session_generation_supported="0", attempt_ids=[attempt],
            parent_attempt_id="-", route_id="rt-native", route_node="execute", receipt=receipt,
            receipt_digest=pending._canonical_receipt_digest(receipt), row_revisions={attempt: revision})
        record = {**pending.read(self.root, sid, delivery_id), "_storage_key": sid}
        turn = {"thread_id": sid, "turn_id": "turn-native", "status": "inProgress"}
        with mock.patch.object(join, "current_attempt_row", return_value=row), \
             mock.patch.object(join, "pending_record_identity",
                               return_value=("rt-native", "execute", "-")), \
             mock.patch("dispatch_seat_handover.effective_parent", return_value=sid), \
             mock.patch("harness_capabilities.parent_completion", return_value={"carrier": carrier}), \
             mock.patch.object(obligations, "ensure_normal_runner"):
            self.assertTrue(obligations.retain_registered_completion(
                self.jobs, attempt, sid, carrier, delivery_record=record, receiving_turn=turn,
                request_context="Exact original request\nOUTPUT_FORMAT_CANARY: exact JSON"))
        duties = obligations.ObligationStore(self.jobs.parent).list(kinds={"registered-batch"})
        self.assertEqual(len(duties), 1)
        retained = duties[0]["observation"]["native_receiving_turn"]
        self.assertEqual((retained["thread_id"], retained["turn_id"], retained["delivery_id"]),
                         (sid, "turn-native", delivery_id))
        context = duties[0]["observation"]["native_request_context"]
        self.assertEqual(context["source"], "codex-userprompt-submit")
        self.assertEqual(context["status"], "available")
        self.assertEqual(context["text"], "Exact original request\nOUTPUT_FORMAT_CANARY: exact JSON")
        self.assertEqual((context["thread_id"], context["turn_id"], context["delivery_id"],
                          context["receipt_digest"], context["row_revision"]),
                         (sid, "turn-native", delivery_id,
                          metadata["delivery_receipt_digest"], revision))
        self.assertEqual(pending.read(self.root, sid, delivery_id)["state"], "pending")
        invalid_turn = {"thread_id": sid, "message_id": "not-a-turn", "status": "inProgress"}
        self.assertFalse(obligations.retain_registered_completion(
            self.jobs, attempt, sid, carrier, delivery_record=record, receiving_turn=invalid_turn))

    def test_native_continuation_preparation_keeps_first_body_for_same_id(self):
        duty = self.store.create("registered-batch-prepared-test", "registered-batch",
                                 {"jobs": str(self.jobs), "attempt_id": "att-prepared",
                                  "session_id": "sid-prepared", "harness": "codex",
                                  "server": "default"}, {"carrier": "codex-native-queue"})
        first = "receipt\nOriginal request and exact output format"
        _record, prepared = self.store.prepare_native_continuation(
            duty["id"], continuation_id="delivery-prepared", source_turn_id="turn-prepared",
            source={"source": "codex-userprompt-submit", "status": "available"},
            message=first)
        record = self.store.update(duty["id"], observation={"reason": "after-send"})
        self.assertEqual(record["observation"]["continuation_prepared"]["message"], first)
        _record, retried = self.store.prepare_native_continuation(
            duty["id"], continuation_id="delivery-prepared", source_turn_id="later-turn",
            source={"source": "unavailable", "status": "unavailable"},
            message="changed retry body")
        self.assertEqual(retried, prepared)
        self.assertEqual(self.store.get(duty["id"])["observation"]["continuation_source"]["source"],
                         "codex-userprompt-submit")

    def test_oversized_hook_prompt_is_unavailable_without_oversizing_the_duty(self):
        duty = self.store.create("registered-batch-large-prompt", "registered-batch",
                                 {"jobs": str(self.jobs), "attempt_id": "att-large-prompt",
                                  "session_id": "sid-large-prompt", "harness": "codex",
                                  "server": "default"}, {"carrier": "codex-native-queue"})
        binding = {"thread_id": "sid-large-prompt", "turn_id": "turn-large-prompt",
                   "delivery_id": "delivery-large-prompt", "receipt_digest": "digest-large",
                   "row_revision": "revision-large", "status": "inProgress"}
        retained = self.store.retain_native_receiving_turn(
            duty["id"], binding, request_context="x" * 140_000)
        self.assertEqual(retained["observation"]["native_request_context"]["status"],
                         "unavailable")
        self.assertEqual(retained["observation"]["native_request_context"]["reason"],
                         "hook-prompt-exceeds-obligation-record-limit")
        reloaded = self.store.get(duty["id"])
        self.assertEqual(reloaded["observation"]["native_receiving_turn"], binding)
        self.assertLessEqual(self.store._record_path(duty["id"]).stat().st_size,
                             obligations._MAX_RECORD_BYTES)

    def _parent_handover(self, harness="codex"):
        import dispatch_supervision as supervision
        import dispatch_pending_delivery as pending
        from harness_capabilities import parent_completion
        carrier = parent_completion(harness)["carrier"]
        metadata = {"attempt_schema_version": "2", "attempt_id": "att-stopped-owner",
            "dispatch_depth": "1", "worker_type": "owner", "parent_sid": "idle-parent",
            "parent_harness": harness, "parent_completion_delivery": carrier,
            "note": "dead-worker-blocked", "delivery_intent": "1", "delivery_id": "delivery-original"}
        self.jobs.write_text("2026-10-18T01:00:00Z\tdone\t/r\t/w\towner\t" +
                             ",".join(f"{k}={v}" for k,v in metadata.items()) + "\n")
        identity = {"jobs": str(self.jobs), "attempt_id": "att-stopped-owner",
                    "session_id": "idle-parent", "harness": harness, "server": "default"}
        old = self.store.create(obligations.stable_duty_id("registered-batch", identity),
                                 "registered-batch", identity, {"carrier": carrier})
        self.store.update(old["id"], state="complete", delivery="acked", cleanup="complete")
        answers = [{"id": "input-retained", "digest": "kept", "text": "continue approved scope"}]
        with mock.patch("dispatch_owner_input.retained", return_value=answers), \
             mock.patch("dispatch_owner_input.blocked_owner_answers", return_value=answers), \
             mock.patch("dispatch_replacement._replacement_in_flight", return_value=False), \
             mock.patch.object(obligations, "ensure_normal_runner") as ensure:
            result = supervision.handover_identity_refusal(self.jobs, "att-stopped-owner",
                {"reason": "replacement-parent-identity-unproven"})
            self.assertTrue(result["parent_notified"])
            records = list(pending.record_directory(self.root, "idle-parent").glob("*.json"))
            record = json.loads(records[0].read_text())
            self.assertTrue(obligations.retain_registered_notice(self.jobs, record))
        self.assertEqual(ensure.call_count, 2)  # repeat producer requests the same runner/duty
        duties = self.store.list(states={"pending"}, kinds={"registered-batch"})
        self.assertEqual(len(duties), 1)
        self.assertEqual(self.store.get(old["id"])["state"], "complete")
        return duties[0], record, answers

    def test_idle_parent_handover_uses_existing_runner_without_a_hook_turn(self):
        import dispatch_pending_delivery as pending
        import dispatch_supervision as supervision
        for harness in ("codex", "claude", "opencode"):
            with self.subTest(harness=harness):
                duty, record, answers = self._parent_handover(harness)
                sent = []
                armed = []
                def courier(args, expected_recipient, on_prepared, producer_binding=None):
                    sent.append((expected_recipient, Path(args.body_file).read_text(), producer_binding))
                    on_prepared("a" * 32)
                    return 0
                census = {"result": {"agents": [{"agent": harness, "pane_id": "w1:p5",
                             "agent_session": {"value": "idle-parent"}}]}}
                with mock.patch("dispatch_owner_input.blocked_owner_answers", return_value=answers), \
                     mock.patch("dispatch_replacement._replacement_in_flight", return_value=False), \
                     mock.patch.object(peer_steward.subprocess, "run",
                         return_value=type("Result", (), {"stdout": json.dumps(census)})()), \
                     mock.patch.object(peer_steward, "_resolve_target", return_value=(harness, "idle-parent")), \
                     mock.patch.object(peer_steward, "cmd_prompt", side_effect=courier), \
                     mock.patch.object(peer_steward.peer_message, "bind_dispatch_notice"), \
                     mock.patch.object(peer_steward.peer_message, "_read_pending", return_value={"state": "queued"}), \
                     mock.patch.object(supervision, "continue_for_parent", side_effect=lambda *a, **k:
                         armed.append(k) or {"state": "armed"}):
                    peer_steward._reconcile_obligations(self.store, [duty])
                    latest = self.store.get(duty["id"])
                    self.assertEqual(latest["observation"]["transfer_ref"], "a" * 32)
                    peer_steward._reconcile_obligations(self.store, [latest])
                    self.assertEqual(len(sent), 1)
                    self.assertEqual(sent[0][0], (harness, "idle-parent"))
                    self.assertIn("scheduled the route's start under the parent's identity", sent[0][1])
                    self.assertEqual(sent[0][2]["schema"], "registered-runtime-completion-v1")
                    self.assertEqual(sent[0][2]["attempt_id"], "att-stopped-owner")
                    self.assertEqual(armed[0]["session_id"], "idle-parent")
                    exact = pending.read(self.root, "idle-parent", record["delivery_id"])
                    self.assertEqual(exact["state"], "sent-ambiguous")
                    # Sending is not ACK. Only the exact recipient receipt settles this notice.
                    pending.ack_received(self.root, "idle-parent", record["delivery_id"],
                        expected_identity=pending.received_ack_identity(exact, "idle-parent", record["delivery_id"]),
                        acked_by="fixture-exact-recipient")
                    peer_steward._reconcile_obligations(self.store, [self.store.get(duty["id"])])
                    self.assertEqual(self.store.get(duty["id"])["state"], "complete")
                    self.assertEqual(len(sent), 1)
                # Each adapter's fixture is distinct; no completed duty is reopened.
                for path in pending.record_directory(self.root, "idle-parent").glob("*.json"):
                    path.unlink()

    def test_obsolete_handover_retires_without_waking_a_parent(self):
        duty, record, answers = self._parent_handover()
        with mock.patch("dispatch_owner_input.blocked_owner_answers", return_value=[]), \
             mock.patch.object(peer_steward, "cmd_prompt") as send:
            peer_steward._reconcile_obligations(self.store, [duty])
        send.assert_not_called()
        self.assertEqual(self.store.get(duty["id"])["state"], "complete")

    def test_new_handover_preserves_a_live_claim_and_recipient_identity(self):
        import dispatch_pending_delivery as pending
        duty, record, answers = self._parent_handover()
        with mock.patch("dispatch_owner_input.blocked_owner_answers", return_value=answers), \
             mock.patch("dispatch_replacement._replacement_in_flight", return_value=False), \
             mock.patch.object(peer_steward.subprocess, "run", return_value=type("Result", (), {
                 "stdout": json.dumps({"result": {"agents": [{"agent": "codex", "pane_id": "w1:p5",
                     "agent_session": {"value": "idle-parent"}}]}})})()), \
             mock.patch.object(peer_steward, "_resolve_target", return_value=("codex", "wrong-parent")), \
             mock.patch.object(peer_steward, "cmd_prompt") as send:
            peer_steward._reconcile_obligations(self.store, [duty])
            self.assertEqual(pending.read(self.root, "idle-parent", record["delivery_id"])["attempts"], 0)
            pending.claim(self.root, "idle-parent", record["delivery_id"], claim_owner="other-live-carrier",
                          lease_seconds=300, require_generation_proof=False, jobs=self.jobs)
            with mock.patch.object(peer_steward, "_resolve_target", return_value=("codex", "idle-parent")):
                peer_steward._reconcile_obligations(self.store, [duty])
        send.assert_not_called()
        self.assertIn(self.store.get(duty["id"])["state"], {"pending", "unknown"})
        self.assertEqual(pending.read(self.root, "idle-parent", record["delivery_id"])["claim_owner"],
                         "other-live-carrier")

    @unittest.skipUnless(hasattr(os, "pidfd_open"), "Linux pidfd handoff")
    def test_unsupported_observer_releases_same_lock_without_losing_accepted_duties(self):
        self._check_observer_handoff(supported=False)

    @unittest.skipUnless(hasattr(os, "pidfd_open"), "Linux pidfd handoff")
    def test_supported_observer_keeps_its_pid_and_same_lock(self):
        self._check_observer_handoff(supported=True)

    @unittest.skipUnless(hasattr(os, "pidfd_open"), "Linux pidfd handoff")
    def test_claude_only_observer_is_replaced_for_shared_activation_delivery(self):
        self._check_observer_handoff(supported=False, native_only=True)

    @unittest.skipUnless(hasattr(os, "pidfd_open"), "Linux pidfd handoff")
    def test_a_legacy_observer_keeps_its_pid_while_current_runner_starts(self):
        self._check_observer_handoff(supported=False, legacy=True)

    def _check_observer_handoff(self, supported, legacy=False, native_only=False):
        duty = self.store.create("registered-batch-fixture", "registered-batch",
                                 {"session_id": "parent"}, {"carrier": "claude-parent-runtime"})
        prior = self.store.create("message-fixture", "message", {"session_id": "other"}, {"ref": "old"})
        script = self.root / "peer-steward.py"
        source = "import time\nprint('ready', flush=True)\ntime.sleep(30)\n"
        if supported:
            source = ("def _resume_registered_obligation():\n"
                      "    from dispatch_session_sweep import addressed_records\n" + source)
        elif native_only:
            source = "def _resume_registered_obligation(): pass\n" + source
        script.write_text(source)
        lock_path = self.store.root / ("runner.lock" if legacy else obligations.RUNNER_LOCK_NAME)
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        process = subprocess.Popen([sys.executable, str(script), "__obligation-runner",
                                    "--state-root", str(self.root), "--lock-fd", str(fd)],
                                   pass_fds=(fd,), stdout=subprocess.PIPE, text=True)
        os.close(fd)
        try:
            self.assertEqual(process.stdout.readline().strip(), "ready")
            with mock.patch.object(obligations.subprocess, "Popen") as spawned, \
                    mock.patch.object(obligations, "_RUNNERS", []):
                self.assertTrue(obligations.ensure_runner(self.root))
                if legacy:
                    self.assertIsNone(process.poll())
                    spawned.assert_called_once()
                    self.assertEqual(len(spawned.call_args.kwargs["pass_fds"]), 1)
                    self.assertNotEqual(lock_path.name, obligations.RUNNER_LOCK_NAME)
                elif supported:
                    self.assertIsNone(process.poll())
                    spawned.assert_not_called()
                else:
                    # A caller without a verified managed lifecycle binding
                    # cannot replace a live process, even if it looks legacy.
                    self.assertIsNone(process.poll())
                    spawned.assert_not_called()
            self.assertEqual(self.store.get(duty["id"]), duty)
            self.assertEqual(self.store.get(prior["id"]), prior)
        finally:
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=5)
            process.stdout.close()

    def test_observer_error_after_fulfillment_cannot_reopen_duty(self):
        self.store.create("retire-done", "retire", {"pane": "w1:p1"},
                          {"target": "worker"})
        done = self.store.update("retire-done", state="complete", result="normal-exit",
                                 observation={"phase": "complete"}, cleanup="complete")
        after_error = self.store.update("retire-done", observer_error="observer-unavailable")
        self.assertEqual(after_error, done)

    def test_conditional_observation_preserves_claim_and_terminal_cancellation(self):
        self.store.create("retire-race", "retire", {"pane": "w1:p1"}, {"target": "peer"})
        claimed = self.store.claim_phase("retire-race", {"waiting"}, "exit-requested",
                                        extra={"foreground": {"pid": 42}})
        stale = self.store.update("retire-race", state="pending", expected_phases={"waiting"},
                                  observation={"phase": "waiting", "reason": "stale"})
        self.assertEqual(stale, claimed)
        cancelled = self.store.update("retire-race", state="cancelled",
                                      observation={"phase": "waiting", "reason": "cancelled"})
        self.assertEqual(self.store.update("retire-race", state="unknown", expected_phases={"waiting"},
                                          observation={"phase": "waiting"}), cancelled)
        self.assertIsNone(self.store.claim_phase("retire-race", {"waiting"}, "exit-requested"))

    def test_unreadable_binding_source_is_unknown(self):
        from peer_read_context import PeerReadContext

        jobs = self.root / "not-a-file"
        jobs.mkdir()
        work, state = obligations.bound_work_for_pane(
            "w1:p4", "codex", "thread-1", jobs=jobs,
            read_context=PeerReadContext(registry_append_only=True),
            registry_append_only=True)
        self.assertEqual((work, state), ((), "unknown"))
        ready = obligations.pane_readiness(
            server="fixture-server", pane="w1:p4", harness="codex",
            session_id="thread-1", pid_birth="pid:9@start:4",
            identity_verified=True, native_turn="idle",
            bound_work=work, bindings_state=state,
        )
        self.assertEqual((ready.state, ready.reason),
                         ("unknown", "registered-bindings-unavailable"))

    def test_invalid_utf8_binding_cannot_turn_missing_work_into_ready(self):
        for harness in ("claude", "codex", "opencode"):
            with self.subTest(harness=harness):
                raw = ("now\topen\trepo\t-\tchild\tattempt_id=att-child,"
                       f"harness={harness},parent_harness={harness},parent_sid=sid").encode()
                raw += b"\xff\n"
                self.assertEqual(len(raw.rstrip(b"\n").split(b"\t")), 6)
                self.jobs.write_bytes(raw)
                work, state = obligations.bound_work_for_pane(
                    "w1:p4", harness, "sid", jobs=self.jobs)
                self.assertEqual((work, state), ((), "unknown"))
                ready = obligations.pane_readiness(
                    server="fixture-server", pane="w1:p4", harness=harness,
                    session_id="sid", pid_birth="pid:9@start:4",
                    identity_verified=True, native_turn="idle",
                    bound_work=work, bindings_state=state)
                self.assertEqual((ready.state, ready.reason),
                                 ("unknown", "registered-bindings-unavailable"))
                self.assertEqual(self.jobs.read_bytes(), raw)

    def test_foreground_tag_binds_executor_even_when_it_also_owns_children(self):
        from dispatch_contract import ObservedAttemptLiveness
        jobs = self.root / "jobs.log"
        for harness in ("claude", "codex", "opencode"):
            jobs.write_text(
                f"now\topen\trepo\t-\texec\tattempt_id=att-exec,harness={harness},parent_sid=other\n"
                f"now\topen\trepo\t-\tchild\tattempt_id=att-child,parent_sid=sid,parent_harness={harness}\n")
            with mock.patch("dispatch_contract.observed_attempt_liveness", return_value=
                            ObservedAttemptLiveness("alive", "fixture", "live", "fixture")):
                work, state = obligations.bound_work_for_pane(
                    "pane", harness, "sid", jobs=jobs, execution_attempt_ids=("att-exec",))
            self.assertEqual(state, "observed")
            self.assertEqual([(w.attempt_id, w.pane_relation) for w in work],
                             [("att-exec", "executor"), ("att-child", "parent")])
            # Input readiness ignores parent rows, so it never observes them.
            with mock.patch("dispatch_contract.observed_attempt_liveness", return_value=
                            ObservedAttemptLiveness("alive", "fixture", "live", "fixture")), \
                 mock.patch("dispatch_terminal_commit.owner_completion_state",
                            wraps=__import__("dispatch_terminal_commit").owner_completion_state) as closure:
                work, state = obligations.bound_work_for_pane(
                    "pane", harness, "sid", jobs=jobs, execution_attempt_ids=("att-exec",),
                    include_parent=False)
            self.assertEqual([(w.attempt_id, w.pane_relation) for w in work], [("att-exec", "executor")])
            self.assertEqual(closure.call_count, 1)

    def test_foreground_tag_read_is_birth_checked_and_does_not_use_parent_tag(self):
        with mock.patch("dispatch_contract._runtime_ancestry_proc_stat", return_value={"start": 7}), \
             mock.patch.object(Path, "read_bytes", return_value=b"AGENT_DISPATCH_ATTEMPT_ID=att-own\0"):
            self.assertEqual(obligations.execution_attempts_for_processes([{"pid": 123}]), ("att-own",))
        with mock.patch("dispatch_contract._runtime_ancestry_proc_stat",
                        side_effect=[{"start": 7}, {"start": 8}]), \
             mock.patch.object(Path, "read_bytes", return_value=b"AGENT_DISPATCH_ATTEMPT_ID=att-own\0"):
            self.assertEqual(obligations.execution_attempts_for_processes([{"pid": 123}]), ())
        with mock.patch("dispatch_contract._runtime_ancestry_proc_stat", return_value={"start": 7}), \
             mock.patch.object(Path, "read_bytes", side_effect=PermissionError()):
            with self.assertRaisesRegex(obligations.ObligationError, "pane-execution-unavailable"):
                obligations.execution_attempts_for_processes([{"pid": 123}])


_FAKE_HERDR = r'''#!/usr/bin/env python3
import json, os, sys
argv = sys.argv[1:]
mode = os.environ.get("FAKE_HERDR_MODE", "idle")
target = argv[2] if len(argv) > 2 else "-"
info = {"result": {"agent": {"agent": "claude", "agent_session": {"value": "sid-fake"},
        "agent_status": "idle", "name": target, "pane_id": "w1:p9",
        "window_id": os.environ.get("FAKE_HERDR_WINDOW_ID", "window-fixture")},
        "type": "agent_info"}}
verb = argv[1] if len(argv) > 1 else ""
if verb == "process-info":
    print(json.dumps({"result": {"process_info": {
        "pane_id": argv[-1], "shell_pid": os.getppid(),
        "foreground_process_group_id": os.getppid(),
        "foreground_processes": [{"pid": os.getppid(), "argv": ["zsh"]}]}}}))
    sys.exit(0)
if verb == "wait" and mode == "held":
    with open(os.environ["FAKE_HERDR_FIFO"], "r") as fh:
        fh.read()
print(json.dumps(info))
sys.exit(0)
'''

_SCRUB_PREFIXES = ("AGENT_DISPATCH_", "AGENT_SESSION_", "AGENT_RUNTIME_",
                   "AGENT_THREAD_", "HERDR_")
_SCRUB_KEYS = ("AGENT_SESSION_ID", "AGENT_SESSION_ROLE", "CLAUDE_CODE_SESSION_ID",
               "CLAUDE_SESSION_ID", "CODEX_THREAD_ID", "CODEX_SESSION_ID",
               "OPENCODE_SESSION_ID", "OPENCODE_DISPATCH_SLUG",
               "AGENT_HERDR_SESSION")


class StewardRecoveryIntegrationTest(unittest.TestCase):
    """Actual steward/controller recovery: fixture transport, death, restart, display."""


def _fixture_env(root: Path, jobs: Path, state: Path, bindir: Path,
                 fifo: Path, session: str, mode: str) -> dict:
    """Fixture transport with inherited runtime/session/dispatch identities removed."""
    env = dict(os.environ)
    for key in tuple(env):
        if key.startswith(_SCRUB_PREFIXES):
            env.pop(key, None)
    for key in _SCRUB_KEYS:
        env.pop(key, None)
    env["AGENT_DISPATCH_JOBS"] = str(jobs)
    env["AGENT_PEER_LEDGER_ROOT"] = str(state)
    env["HOME"] = str(root / "home")
    env["CLAUDE_CODE_SESSION_ID"] = session
    env["FAKE_HERDR_MODE"] = mode
    env["FAKE_HERDR_FIFO"] = str(fifo)
    env["PATH"] = str(bindir) + os.pathsep + env.get("PATH", "")
    return env


class StewardRecoveryIntegrationTest(unittest.TestCase):
    """Actual steward/controller recovery: fixture transport, death, restart, display."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.jobs = self.root / "jobs.log"
        self.jobs.touch()
        self.state = self.root / "peer-state"
        self.bindir = self.root / "fakebin"
        self.bindir.mkdir()
        fake = self.bindir / "herdr"
        fake.write_text(_FAKE_HERDR, encoding="utf-8")
        fake.chmod(0o755)
        self.fifo = self.root / "release.fifo"
        os.mkfifo(self.fifo)
        self.session = "steward-obligation-1"
        self.saved_environ = dict(os.environ)
        self.addCleanup(self._restore_environ)
        self._children: list[int] = []
        self.addCleanup(self._reap_children)
        self._old_herdr_session = peer_steward._HERDR_SESSION
        peer_steward._HERDR_SESSION = None
        self.addCleanup(self._restore_herdr_session)

    def _restore_environ(self):
        os.environ.clear()
        os.environ.update(self.saved_environ)

    def _restore_herdr_session(self):
        peer_steward._HERDR_SESSION = self._old_herdr_session

    def _reap_children(self):
        for pid in self._children:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        deadline = time.monotonic() + 10
        remaining = set(self._children)
        while remaining and time.monotonic() < deadline:
            for pid in tuple(remaining):
                try:
                    waited, _ = os.waitpid(pid, os.WNOHANG)
                except ChildProcessError:
                    remaining.discard(pid)
                    continue
                except OSError:
                    continue
                if waited:
                    remaining.discard(pid)
            time.sleep(0.02)

    def _apply_fixture_env(self, mode="held"):
        os.environ.clear()
        os.environ.update(_fixture_env(
            self.root, self.jobs, self.state, self.bindir,
            self.fifo, self.session, mode))

    def _arm_watch(self, mode="held"):
        env = _fixture_env(self.root, self.jobs, self.state, self.bindir,
                           self.fifo, self.session, mode)
        proc = subprocess.run(
            [sys.executable, str(_HERE / "peer-steward.py"), "watch", "peer-a"],
            capture_output=True, text=True, env=env, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        fields = {}
        for token in proc.stdout.split():
            key, separator, value = token.partition("=")
            if separator:
                fields[key] = value
        return fields["watch_id"], int(fields["pid"]), env

    @staticmethod
    def _wait_for_death(pid: int, seconds: float = 10.0):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except OSError:
                return
            time.sleep(0.02)
        raise AssertionError(f"fixture observer {pid} never died")

    def test_watch_duty_survives_observer_death_and_resumes_same_id(self):
        watch_id, pid, _env = self._arm_watch(mode="held")
        arm_path = self.state / "peer-watches" / f"{watch_id}.json"
        first = json.loads(arm_path.read_text(encoding="utf-8"))
        self.assertEqual(first["watch_id"], watch_id)
        os.kill(pid, signal.SIGKILL)
        self._wait_for_death(pid)

        self._apply_fixture_env(mode="held")
        spawned = []
        real_popen = subprocess.Popen

        def tracked_spawn(*args, **kwargs):
            process = real_popen(*args, **kwargs)
            spawned.append(process)
            return process

        def reap_spawned():
            for process in spawned:
                if process.poll() is None:
                    process.terminate()
                process.wait(timeout=5)

        self.addCleanup(reap_spawned)
        with mock.patch.object(peer_steward.subprocess, "Popen", side_effect=tracked_spawn):
            peer_steward._ensure_watch_observers()

        resumed = json.loads(arm_path.read_text(encoding="utf-8"))
        self.assertEqual(resumed["watch_id"], watch_id,
                         "restart replaces only the observer, never the duty")
        self.assertGreaterEqual(resumed.get("observer_generation", 1), 2)
        new_pid = int((resumed.get("watcher") or {})["pid"])
        self.assertNotEqual(new_pid, pid)
        try:
            os.kill(new_pid, 0)
        except OSError:
            self.fail("resumed observer is not alive")
        # Death never fulfills the duty: no terminal receipt was written.
        self.assertFalse((self.state / "peer-watches" / f"{watch_id}.receipt.json").exists())

    def test_pending_duties_reach_the_read_only_collector_body_free(self):
        watch_id, pid, _env = self._arm_watch(mode="held")
        self._children.append(pid)
        store = obligations.ObligationStore(self.state)
        store.create("message-" + "r" * 32, "message",
                     {"server": "default", "pane": "w1:p9",
                      "harness": "claude", "session_id": "sid-fake"},
                     {"ref": "r" * 32, "body_digest": "d" * 64,
                      "target": "peer-a",
                      "to": {"harness": "claude", "session_id": "sid-fake"}})
        tools = str(_HERE.parent / "tools")
        if tools not in sys.path:
            sys.path.insert(0, tools)
        from fleet.collectors import peer_messages
        result = peer_messages.collect(state_roots=[str(self.state)])
        pending = result["by_session"][("claude", "sid-fake")]["pending_obligations"]
        kinds = {item["kind"] for item in pending}
        self.assertIn("watch", kinds)
        self.assertIn("message", kinds)
        for item in pending:
            self.assertLessEqual(set(item), {"kind", "state", "age_min", "ref"})
        os.kill(pid, signal.SIGKILL)
        self._wait_for_death(pid)
        self._children.remove(pid)

    def test_fleet_cancelled_child_no_longer_keeps_parent_pane_unknown(self):
        from dispatch_contract import ObservedAttemptLiveness
        attempt = "att-f3daf5ab6f9a367410542ca045fd243d"
        sid = "01a13de1-42a4-7e81-b64f-05839c030e39"
        metadata = {"attempt_schema_version": "2", "attempt_id": attempt,
                    "harness": "opencode", "worker_type": "frame", "route_node": "frame",
                    "dispatch_depth": "1", "parent_sid": sid, "launch_started": "1",
                    "note": "fleet-kill", "failure_class": "cancelled",
                    "classifier_source": "fleet-cancel-v1", "reconcile_reason": "user-terminated"}
        self.jobs.write_text("2026-10-20T00:00:00Z\tdone\trepo\t-\topencode-v2-worker-closure-frame\t"
                             + ",".join(f"{k}={v}" for k, v in metadata.items()) + "\n")
        before = self.jobs.read_bytes()
        for process_state, expected in (("quiescent", "ready"), ("live", "pending"),
                                        ("unverifiable", "unknown")):
            with self.subTest(process_state=process_state), mock.patch(
                    "dispatch_contract.observed_attempt_liveness", return_value=ObservedAttemptLiveness(
                        "dead" if process_state == "quiescent" else
                        "alive" if process_state == "live" else "unverifiable",
                        "fixture", process_state, "fixture")):
                work, state = obligations.bound_work_for_pane("wB:p89", "codex", sid, jobs=self.jobs)
                self.assertEqual((len(work), state), (1, "observed"))
                self.assertEqual(work[0].pane_relation, "parent")
                self.assertEqual(work[0].decision.outcome, "cancelled")
                ready = obligations.pane_readiness(
                    server="fixture-server", pane="wB:p89", harness="codex", session_id=sid,
                    pid_birth="pid:9@start:4", identity_verified=True, native_turn="idle",
                    bound_work=work, bindings_state=state)
                self.assertEqual(ready.state, expected)
                self.assertEqual(self.jobs.read_bytes(), before)
                if expected == "ready":
                    self.assertEqual(ready.outcome, "cancelled")

    def test_bound_registered_row_overrides_idle_pane_through_real_jobs(self):
        from dispatch_contract import (ATTEMPT_SCHEMA_VERSION, claim_attempt_row,
                                       process_launch_identity, ObservedAttemptLiveness)
        metadata = {"attempt_schema_version": str(ATTEMPT_SCHEMA_VERSION),
                    "dispatch_depth": "1", "transport": "headless",
                    "execution_surface": "registered-headless", "registered_worker": "1",
                    "fallback_hop": "same-harness-headless", "harness": "codex",
                    "worker_type": "dev/backend", "owner_route_id": "route-fixture",
                    "owner_route_hash": "sha256:" + "a" * 64, "route_node": "execute",
                    "attempt_id": "att-fixture-1", "parent_sid": "sid-1",
                    "parent_harness": "codex", **process_launch_identity(os.getpid())}
        self.assertNotIn("parent_pane", metadata)
        row = "2026-10-09T00:00:00Z\topen\trepo\t-\tslug\t" + ",".join(
            f"{key}={value}" for key, value in metadata.items())
        self.assertTrue(claim_attempt_row(self.jobs, "att-fixture-1", row, launch=True))
        for process_state, expected in (("live", "pending"), ("unverifiable", "unknown")):
            with self.subTest(process_state=process_state), mock.patch(
                    "dispatch_contract.observed_attempt_liveness", return_value=ObservedAttemptLiveness(
                        "alive" if process_state == "live" else "unverifiable",
                        "fixture", process_state, "fixture")):
                work, state = obligations.bound_work_for_pane(
                    "w1:p4", "codex", "sid-1", jobs=self.jobs)
                self.assertEqual((len(work), state), (1, "observed"))
                ready = obligations.pane_readiness(
                    server="fixture-server", pane="w1:p4", harness="codex",
                    session_id="sid-1", pid_birth="pid:9@start:4",
                    identity_verified=True, native_turn="idle", bound_work=work,
                    bindings_state=state)
                self.assertEqual(ready.state, expected)
                self.assertIsNone(ready.outcome)
        for harness, sid in (("codex", "unrelated"), ("claude", "sid-1")):
            self.assertEqual(obligations.bound_work_for_pane(
                "w1:p4", harness, sid, jobs=self.jobs), ((), "observed"))
        with mock.patch("dispatch_seat_handover._handover_binding_keys_for_session",
                        return_value=(("route-fixture", "sha256:" + "a" * 64, "execute"),)), \
             mock.patch("dispatch_seat_handover.effective_parent", return_value="successor"), \
             mock.patch("dispatch_seat_handover.effective_parent_harness", return_value="claude"):
            work, state = obligations.bound_work_for_pane(
                "w2:p7", "claude", "successor", jobs=self.jobs)
            self.assertEqual((len(work), state), (1, "observed"))

    def test_parent_watch_uses_exact_delivery_without_rechecking_owner_closure(self):
        from dispatch_contract import ATTEMPT_SCHEMA_VERSION, ObservedAttemptLiveness
        import dispatch_pending_delivery as pending
        jobs = self.root / "parent-delivery-jobs.log"
        receipt = {"schema_version": 1, "attempt_id": "att-parent-delivery",
                   "route_id": "rt-parent-delivery", "route_node": "execute"}
        receipt_digest = pending._canonical_receipt_digest(receipt)
        metadata = {
            "attempt_schema_version": str(ATTEMPT_SCHEMA_VERSION), "dispatch_depth": "1",
            "transport": "headless", "execution_surface": "registered-headless",
            "registered_worker": "1", "fallback_hop": "same-harness-headless",
            "harness": "codex", "worker_type": "dev/backend",
            "owner_route_id": "rt-parent-delivery", "owner_route_hash": "sha256:" + "b" * 64,
            "route_node": "execute", "attempt_id": "att-parent-delivery", "parent_sid": "sid-a",
            "parent_harness": "codex", "failure_class": "pass", "note": "completed-marker",
            "delivery_intent": "1", "delivery_id": "delivery-parent",
            "delivery_receipt_digest": receipt_digest,
            "delivery_row_revision": "revision-parent",
            "parent_completion_delivery": "codex-native-queue",
        }
        jobs.write_text("2026-10-20T00:00:00Z\tdone\trepo\t-\tchild\t" + ",".join(
            f"{key}={value}" for key, value in metadata.items()) + "\n")
        pending.create(
            root=jobs.parent, recipient_kind="codex-native-queue", recipient_key="sid-a",
            delivery_id="delivery-parent", session_generation="",
            session_generation_supported="0", attempt_ids=["att-parent-delivery"],
            parent_attempt_id="att-parent-owner", route_id="rt-parent-delivery",
            route_node="execute", receipt=receipt,
            receipt_digest=receipt_digest,
            row_revisions={"att-parent-delivery": "revision-parent"},
        )

        def observe():
            with mock.patch("dispatch_seat_handover._handover_binding_keys_for_session",
                            return_value=(("rt-parent-delivery", "sha256:" + "b" * 64, "execute"),)), \
                 mock.patch("dispatch_seat_handover.effective_parent", return_value="sid-current"), \
                 mock.patch("dispatch_seat_handover.effective_parent_harness", return_value="codex"), \
                 mock.patch("dispatch_contract.observed_attempt_liveness", return_value=
                            ObservedAttemptLiveness("dead", "fixture", "quiescent", "fixture")), \
                 mock.patch("dispatch_terminal_commit.owner_completion_state",
                            side_effect=AssertionError(
                                "parent readiness must not inspect owner closure")):
                work, state = obligations.bound_work_for_pane(
                    "w3:p7", "codex", "sid-current", jobs=jobs)
            ready = obligations.pane_readiness(
                server="fixture", pane="w3:p7", harness="codex",
                session_id="sid-current", pid_birth="pid:9@start:4",
                identity_verified=True, native_turn="idle", bound_work=work,
                bindings_state=state)
            return work, state, ready

        work, state, ready = observe()
        self.assertEqual((len(work), state), (1, "observed"))
        self.assertEqual((work[0].pane_relation, work[0].delivery_state), ("parent", "pending"))
        self.assertEqual(ready.state, "pending")

        pending.claim(jobs.parent, "sid-a", "delivery-parent",
                      claim_owner="parent-watch-test", lease_seconds=30)
        claimed_work, _claimed_state, claimed_ready = observe()
        self.assertEqual((claimed_work[0].delivery_state, claimed_ready.state),
                         ("claimed", "pending"))

        pending.ack(jobs.parent, "sid-a", "delivery-parent", acked_by="recipient-consumed")
        acked_work, _acked_state, acked_ready = observe()
        self.assertEqual((acked_work[0].delivery_state, acked_ready.state), ("acked", "ready"))

        # A fresh ACK cannot satisfy a different result revision in jobs.log.
        fields = jobs.read_text(encoding="utf-8").rstrip("\n").split("\t")
        fields[5] = fields[5].replace(
            "delivery_row_revision=revision-parent",
            "delivery_row_revision=revision-replaced")
        jobs.write_text("\t".join(fields) + "\n", encoding="utf-8")
        mismatched_work, _mismatch_state, mismatched_ready = observe()
        self.assertEqual((mismatched_work[0].delivery_state, mismatched_ready.state),
                         ("unknown", "unknown"))


if __name__ == "__main__":
    unittest.main()
