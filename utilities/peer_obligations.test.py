#!/usr/bin/env python3
"""Temporary-root checks for durable peer obligations and native readiness."""

from __future__ import annotations

import fcntl
import contextlib
import errno
import importlib.util
import json
import os
import shutil
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
from dispatch_contract import pidfd_open
import dispatch_batch_obligations as batch_obligations

_HERE = Path(__file__).resolve().parent
_STEWARD_SPEC = importlib.util.spec_from_file_location(
    "peer_steward_under_obligation_test", str(_HERE / "peer-steward.py"))
peer_steward = importlib.util.module_from_spec(_STEWARD_SPEC)
_STEWARD_SPEC.loader.exec_module(peer_steward)


def _pidfd_handoff_available():
    if not callable(getattr(signal, "pidfd_send_signal", None)):
        return False
    try:
        fd = pidfd_open(os.getpid())
    except OSError as exc:
        if exc.errno == errno.ENOSYS:
            return False
        raise
    try:
        signal.pidfd_send_signal(fd, 0)
    except OSError as exc:
        if exc.errno == errno.ENOSYS:
            return False
        raise
    finally:
        os.close(fd)
    return True


_PIDFD_HANDOFF = _pidfd_handoff_available()


def _legacy_notice(harness, sid, *, key, subject, text, jobs, route_id="route-free",
                   required_action="read-notice"):
    """Historical private notice tag and duty, as stored before the zero-attempt writer; no registry row."""
    import hashlib
    import dispatch_pending_delivery as pending
    import session_notice
    receipt = session_notice.validate({"schema_version": 1, "kind": "notice", "key": key,
        "subject": subject, "text": " ".join(text.split())[:600], "required_action": required_action})
    from harness_capabilities import parent_completion
    delivery_id = "delivery-" + hashlib.sha256(f"notice\0{key}".encode()).hexdigest()
    tag = "notice-" + delivery_id.removeprefix("delivery-")[:32]
    root = Path(jobs).parent
    record = pending.read(root, sid, delivery_id)
    if record is None:
        record = pending.create(root, recipient_kind=parent_completion(harness)["carrier"],
            recipient_key=sid, delivery_id=delivery_id, session_generation="",
            session_generation_supported="0", attempt_ids=[tag], parent_attempt_id=tag,
            route_id=route_id, route_node="notice", receipt=receipt,
            receipt_digest=session_notice.receipt_digest(receipt), row_revisions={tag: "notice"}, lineage=[])
    import peer_obligations
    # Historical retention failure leaves the original record for discovery.
    peer_obligations.retain_session_notice(Path(jobs), harness, sid, record)
    return record


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

    def registered_transfer(self, harness="codex", *, prepare=True, member_count=2, suffix=""):
        """Two exact routes and their real retained producer/dispatch bindings."""
        import dispatch_pending_delivery as pending
        from route_identity import route_hash
        from harness_capabilities import parent_completion
        carrier = parent_completion(harness)["carrier"]
        label = harness + suffix
        sid = "retained-" + label
        members, duties, metas = [], [], []
        for index in range(member_count):
            aid, did, rid = (f"att-retained-{label}-{index}",
                             f"delivery-retained-{label}-{index}", f"rt-retained-{label}-{index}")
            route = {"route_id": rid, "parent_cycle_id": "cycle-retained-" + label,
                     "nodes": [{"id": "frame"}]}
            route["route_hash"] = route_hash(route)
            rp = self.root / (rid + ".json")
            rp.write_text(json.dumps(route))
            body = {"schema_version": 2, "state": "ready", "parent_attempt_id": "att-parent",
                    "job_registry": str(self.jobs), "delivery_classification": "success",
                    "children": [{"attempt_id": aid, "status": "done", "readiness": "ready",
                        "reason": "terminal-complete", "required_action": "advance-completed",
                        "harness": harness, "delivery_classification": "success"}]}
            record = pending.create(self.root, recipient_kind=carrier, recipient_key=sid,
                delivery_id=did, session_generation="", session_generation_supported="0",
                attempt_ids=[aid], parent_attempt_id="att-parent", route_id=rid, route_node="frame",
                receipt=body, receipt_digest=pending._canonical_receipt_digest(body), row_revisions={aid: "revision"})
            meta = dict(attempt_id=aid, attempt_schema_version="2", dispatch_depth="1",
                registered_worker="1", worker_type="frame", parent_sid=sid, parent_harness=harness,
                parent_attempt_id="att-parent", parent_completion_delivery=carrier,
                delivery_intent="1", delivery_intent_at_ns=str(time.monotonic_ns()),
                delivery_id=did, delivery_receipt_digest=record["receipt_digest"],
                delivery_row_revision="revision", failure_class="pass", note="completed-marker",
                launch_outcome="reaped-before-publish", route_file=str(rp), route_id=rid,
                route_hash=route["route_hash"], route_node="frame")
            with self.jobs.open("a") as handle:
                handle.write("now\tdone\t/fixture/repo\t/fixture/wt\tframe\t" +
                             ",".join(key + "=" + value for key, value in meta.items()) + "\n")
            identity = dict(jobs=str(self.jobs), attempt_id=aid, session_id=sid,
                            harness=harness, server="default")
            duty = self.store.create(obligations.stable_duty_id("registered-batch", identity),
                                     "registered-batch", identity, {"carrier": carrier})
            members.append(record)
            duties.append(duty)
            metas.append(meta)
        if not prepare:
            return sid, members, duties, metas
        owner = "retained-rewake:" + duties[0]["id"]
        claimed = pending.claim_ready_batch(self.root, sid, members[0]["delivery_id"],
            claim_owner=owner, lease_seconds=30, require_generation_proof=False, jobs=self.jobs)
        self.assertEqual(len(claimed), member_count)
        recipient = {"harness": harness, "session_id": sid}
        bindings = [dict(duty_id=duty["id"], jobs=str(self.jobs), attempt_id=meta["attempt_id"],
            carrier=carrier, recipient_sid=sid, storage_recipient=sid,
            delivery_id=record["delivery_id"], receipt_digest=record["receipt_digest"], claim_owner=owner)
            for duty, meta, record in zip(duties, metas, members)]
        producer = dict(schema="registered-runtime-completion-v1", duty_id=duties[0]["id"],
            jobs=str(self.jobs), attempt_id=metas[0]["attempt_id"], carrier=carrier,
            delivery_id=members[0]["delivery_id"], receipt_digest=members[0]["receipt_digest"],
            recipient=recipient, route_id=metas[0]["route_id"], route_node="frame",
            dispatch_notices=[{key: binding[key] for key in ("duty_id", "attempt_id", "delivery_id",
                "storage_recipient", "receipt_digest", "claim_owner")} for binding in bindings])
        refs = [value for binding in bindings for value in (
            binding["duty_id"], binding["attempt_id"], binding["delivery_id"])]
        message = "\n".join(__import__("dispatch_recipient_receipt").receipt_marker(record, sid)
                             for record in claimed)
        peer = peer_steward.peer_message
        _text, ref = peer.prepare_peer_message(message,
            {"harness": "runtime", "session_id": None, "name": "Hearting runtime"},
            recipient, defer=True, refs=refs, producer_binding=producer)
        peer.bind_dispatch_notice(ref, bindings)
        transfer = peer._read_pending(ref)
        self.assertEqual(len(list(obligations._registered_delivery_bindings(transfer))), member_count)
        return ref, recipient, claimed, metas

    def resume_registered_case(self, late):
        import dispatch_pending_delivery as pending
        import dispatch_seat_handover as handover
        import session_tidy as tidy
        sid, records, duties, metas = self.registered_transfer("codex", prepare=False)
        sent = []
        def continued(record, **_kwargs):
            if late and record["delivery_id"] == records[-1]["delivery_id"]:
                tidy._append_ledger_entry(tidy.Seat("pane", "retained-late-member"),
                    dict(event="handover", source="retire", harness="codex", sid="new-holder",
                         **{"from": sid}, ts=1, bindings=[handover.binding_of(self.jobs, metas[0])]))
        def courier(args, *, expected_recipient, on_prepared, producer_binding):
            body = Path(args.body_file).read_text()
            peer = peer_steward.peer_message
            _text, ref = peer.prepare_peer_message(body,
                {"harness": "runtime", "session_id": None, "name": "Hearting runtime"},
                {"harness": expected_recipient[0], "session_id": expected_recipient[1]},
                defer=True, refs=args.ref, producer_binding=producer_binding)
            on_prepared(ref)
            transfer = peer._read_pending(ref)
            self.assertEqual(len(list(obligations._registered_delivery_bindings(transfer))), 2)
            self.assertTrue(obligations.registered_submission_current(transfer))
            sent.append(transfer)
            return 3
        agents = {"result": {"agents": [{"agent": "codex", "pane_id": "w1:pRetained",
            "agent_session": {"value": sid}}]}}
        with mock.patch.object(peer_steward.subprocess, "run", return_value=type("Result", (), {
                "stdout": json.dumps(agents), "returncode": 0})()), \
                mock.patch.object(peer_steward, "_resolve_target", return_value=("codex", sid)), \
                mock.patch.object(peer_steward, "cmd_prompt", side_effect=courier), \
                mock.patch("dispatch_notice_state.keep_claim", return_value=True), \
                mock.patch("dispatch_supervision.continue_for_parent", side_effect=continued):
            peer_steward._resume_registered_obligation(duties[0], self.store)
        self.assertEqual(len(sent), 0 if late else 1)
        for record in records:
            current = pending.read(self.root, sid, record["delivery_id"])
            self.assertEqual(current["state"], "pending" if late else "sent-ambiguous")
            self.assertEqual(pending.received_ack_identity(current, sid, record["delivery_id"]),
                             pending.received_ack_identity(record, sid, record["delivery_id"]))
        if sent:
            self.assertEqual(len(sent[0]["dispatch_notices"]), 2)
            self.assertTrue(all(record["delivery_id"] in sent[0]["text"] for record in records))

    def test_registered_resume_producer_binds_every_real_route_member(self):
        self.resume_registered_case(False)

    def test_registered_resume_holds_group_after_later_member_continuation_redirects_first(self):
        self.resume_registered_case(True)

    def test_registered_same_sid_cross_harness_handover_reaches_current_pair(self):
        import dispatch_pending_delivery as pending
        import dispatch_seat_handover as handover
        import session_tidy as tidy
        for source, target in (("claude", "codex"), ("codex", "claude")):
            with self.subTest(source=source, target=target):
                sid, records, duties, metas = self.registered_transfer(source, prepare=False)
                tidy._append_ledger_entry(tidy.Seat("pane", "same-sid-pair-" + source),
                    dict(event="handover", source="retire", harness=target, sid=sid,
                         **{"from": {"harness": source, "sid": sid}}, ts=1,
                         bindings=[handover.binding_of(self.jobs, meta) for meta in metas]))
                self.assertEqual(handover.record_recipient_binding(records[0], sid,
                    harness=source, jobs=self.jobs), (target, sid))
                sent = []
                def courier(args, *, expected_recipient, on_prepared, producer_binding):
                    self.assertEqual(expected_recipient, (target, sid))
                    peer = peer_steward.peer_message
                    _text, ref = peer.prepare_peer_message(Path(args.body_file).read_text(),
                        {"harness": "runtime", "session_id": None, "name": "Hearting runtime"},
                        {"harness": target, "session_id": sid}, defer=True, refs=args.ref,
                        producer_binding=producer_binding)
                    on_prepared(ref)
                    transfer = peer._read_pending(ref)
                    self.assertEqual(len(list(obligations._registered_delivery_bindings(transfer))), 2)
                    self.assertTrue(obligations.registered_submission_current(transfer))
                    sent.append(transfer)
                    return 3
                agents = {"result": {"agents": [{"agent": harness, "pane_id": "w1:p-" + harness,
                    "agent_session": {"value": sid}} for harness in (source, target)]}}
                with mock.patch.object(peer_steward.subprocess, "run", return_value=type("Result", (), {
                        "stdout": json.dumps(agents), "returncode": 0})()), \
                        mock.patch.object(peer_steward, "_resolve_target", side_effect=lambda pane, *_a, **_kw:
                            (pane.removeprefix("w1:p-"), sid)), \
                        mock.patch.object(peer_steward, "_settle_claude_native_dispatch_receipt", return_value=False), \
                        mock.patch.object(peer_steward, "cmd_prompt", side_effect=courier), \
                        mock.patch("dispatch_notice_state.keep_claim", return_value=True):
                    peer_steward._resume_registered_obligation(duties[0], self.store)
                self.assertEqual(len(sent), 1)
                self.assertEqual(sent[0]["to"], {"harness": target, "session_id": sid})
                self.assertTrue(all(pending.read(self.root, sid, record["delivery_id"])["state"]
                                    == "sent-ambiguous" for record in records))

    def test_registered_deferred_write_and_native_add_check_every_bound_member(self):
        import dispatch_pending_delivery as pending
        import dispatch_seat_handover as handover
        import session_tidy as tidy
        import codex_queue_delivery as queue
        peer = peer_steward.peer_message
        for harness in ("claude", "codex", "opencode"):
            ref, recipient, records, metas = self.registered_transfer(harness)
            def move(index):
                tidy._append_ledger_entry(tidy.Seat("pane", "retained-away-" + harness),
                    dict(event="handover", source="retire", harness=harness,
                         sid="next-holder", **{"from": recipient["session_id"]}, ts=time.monotonic_ns(),
                         bindings=[handover.binding_of(self.jobs, metas[index])]))
            if harness == "codex":
                added = []
                def send(*args, **kwargs):
                    move(0)
                    kwargs["before_add"]()
                    added.append(kwargs["message"])
                    return {"status": "queued", "queued_submission_id": "q"}
                with mock.patch("codex_queue_dispatch.resolve_queue_socket", return_value=self.root / "native.sock"), \
                        mock.patch.object(queue, "send_at_least_once", side_effect=send):
                    result = peer.deliver_pending_codex(ref)
                self.assertEqual(result["status"], "unverified")
                self.assertEqual(added, [])
            else:
                reserved = peer.reserve_pending_herdr(ref, recipient)
                self.assertIsNotNone(reserved)
                move(1)
                self.assertIsNone(peer.begin_pending_write(reserved, recipient=recipient))
            self.assertNotEqual(peer._read_pending(ref)["state"], "received")
            self.assertTrue(all(pending.read(self.root, recipient["session_id"], record["delivery_id"])["state"]
                                != "acked" for record in records))

    def test_registered_deferred_transfer_renews_only_its_unsent_exact_claim(self):
        import dispatch_pending_delivery as pending
        peer = peer_steward.peer_message
        ref, recipient, records, _metas = self.registered_transfer("claude")
        for record in records:
            path = pending.record_path(self.root, recipient["session_id"], record["delivery_id"])
            raw = json.loads(path.read_text())
            raw.update(state="sent-ambiguous", claim_deadline_ns=time.monotonic_ns() - 1)
            path.write_text(json.dumps(raw))
        reserved = peer.reserve_pending_herdr(ref, recipient)
        self.assertIsNotNone(reserved)
        self.assertEqual(peer.begin_pending_write(reserved, recipient=recipient)["state"], "unverified")
        self.assertIsNone(peer.begin_pending_write(reserved, recipient=recipient))
        for record in records:
            fresh = pending.read(self.root, recipient["session_id"], record["delivery_id"])
            self.assertGreater(fresh["claim_deadline_ns"], time.monotonic_ns())
            self.assertEqual(fresh["attempts"], record["attempts"])
            self.assertEqual(pending.received_ack_identity(fresh, recipient["session_id"], record["delivery_id"]),
                             pending.received_ack_identity(record, recipient["session_id"], record["delivery_id"]))

    def test_registered_pending_body_keeps_original_identity_at_both_write_boundaries(self):
        import dispatch_pending_delivery as pending
        import codex_queue_delivery as queue
        peer = peer_steward.peer_message
        for harness, index in (("claude", 0), ("codex", 1)):
            with self.subTest(harness=harness, index=index):
                ref, recipient, records, _metas = self.registered_transfer(harness)
                reserved = peer.reserve_pending_herdr(ref, recipient) if harness == "claude" else None
                path = pending.record_path(self.root, recipient["session_id"], records[index]["delivery_id"])
                raw = json.loads(path.read_text())
                raw["route_node"] = "changed-original-identity"
                path.write_text(json.dumps(raw))
                before = path.read_bytes()
                if reserved:
                    self.assertIsNone(peer.begin_pending_write(reserved, recipient=recipient))
                else:
                    added = []
                    def send(*_args, **kwargs):
                        kwargs["before_add"]()
                        added.append(kwargs["message"])
                        return {"status": "queued", "queued_submission_id": "q"}
                    with mock.patch("codex_queue_dispatch.resolve_queue_socket", return_value=self.root / "native.sock"), \
                            mock.patch.object(queue, "send_at_least_once", side_effect=send):
                        result = peer.deliver_pending_codex(ref)
                    self.assertEqual(result["status"], "unverified")
                    self.assertEqual(added, [])
                self.assertEqual(path.read_bytes(), before)
                self.assertEqual(peer._read_pending(ref)["state"], "pending")

    def test_registered_reference_payload_keeps_original_marker_identity(self):
        import dispatch_pending_delivery as pending
        import codex_queue_delivery as queue
        peer = peer_steward.peer_message
        ref, recipient, records, _metas = self.registered_transfer("codex")
        original = peer._read_pending(ref)
        body = original["text"].split("\n\n(runtime-from:", 1)[0] + "\n" + "x" * 20000
        with mock.patch.object(peer, "_reference_access_evidence_valid", return_value=True):
            _text, referenced = peer.prepare_peer_message(body, original["from"], recipient,
                defer=True, refs=original["refs"], producer_binding=original["producer_binding"],
                reference_evidence={"fixture": "private-source-read-access"})
        peer.bind_dispatch_notice(referenced, original["dispatch_notices"])
        message = peer._read_pending(referenced)
        self.assertIsNotNone(message.get("body_ref"))
        self.assertTrue(peer._registered_submission_current(message))
        path = pending.record_path(self.root, recipient["session_id"], records[-1]["delivery_id"])
        raw = json.loads(path.read_text())
        raw["route_node"] = "changed-reference-identity"
        path.write_text(json.dumps(raw))
        added = []
        def send(*_args, **kwargs):
            kwargs["before_add"]()
            added.append(kwargs["message"])
            return {"status": "queued", "queued_submission_id": "q"}
        with mock.patch("codex_queue_dispatch.resolve_queue_socket", return_value=self.root / "native.sock"), \
                mock.patch.object(queue, "send_at_least_once", side_effect=send):
            result = peer.deliver_pending_codex(referenced)
        self.assertEqual(result["status"], "unverified")
        self.assertEqual(added, [])

    def test_registered_hook_context_recovery_preserves_existing_native_boundary(self):
        peer = peer_steward.peer_message
        ref, recipient, _records, _metas = self.registered_transfer("claude")
        original = peer._read_pending(ref)
        for state, reason in (("queued", "native-queue-accepted"), ("unverified", "native-inflight")):
            peer._save_pending(dict(original, state=state, receipt=reason))
            self.assertFalse(obligations.registered_submission_current(peer._read_pending(ref)))
        peer._save_pending(dict(original, state="queued", receipt="hook-context-emitted"))
        reserved = peer.reserve_pending_herdr(ref, recipient, receipt="hook-context-inflight",
                                              allow_hook_context_recovery=True)
        self.assertIsNotNone(reserved)
        self.assertEqual(peer.begin_pending_write(reserved, recipient=recipient,
            receipt="hook-context-inflight")["state"], "unverified")

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

    def test_lifecycle_initializer_creates_one_root_beneath_an_existing_parent(self):
        parent = self.root / "existing-parent"
        parent.mkdir()
        state_root = parent / "state"
        store = obligations.ObligationStore(state_root, allow_state_root_creation=True)

        self.assertTrue(store._initialize_state_root_for_producer(create_parents=False))
        self.assertTrue(state_root.is_dir())
        self.assertEqual(state_root.stat().st_mode & 0o777, 0o700)
        self.assertTrue(store._state_root_identity_is_current())
        self.assertTrue(store.ensure_directory())

    def test_lifecycle_initializer_refuses_missing_parent_and_replaced_root(self):
        missing_root = self.root / "missing-parent" / "state"
        missing = obligations.ObligationStore(
            missing_root, allow_state_root_creation=True)
        self.assertFalse(missing._initialize_state_root_for_producer(create_parents=False))
        self.assertFalse(missing_root.parent.exists())

        state_root = self.root / "observed-state"
        state_root.mkdir()
        observed = obligations.ObligationStore(
            state_root, allow_state_root_creation=True)
        moved = state_root.with_name("observed-state-held")
        state_root.rename(moved)
        state_root.mkdir(mode=0o700)

        self.assertFalse(observed._initialize_state_root_for_producer(create_parents=False))
        self.assertFalse(observed.ensure_directory())
        with self.assertRaises(obligations.ObligationError):
            observed.create("replacement-must-stay-empty", "message", {}, {})
        self.assertEqual(list(state_root.iterdir()), [])
        self.assertEqual(list(moved.iterdir()), [])

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
        with mock.patch.object(obligations.time, "monotonic", return_value=0.0):
            result = self.store.prune_terminal_obligations(now=now)
        self.assertLessEqual(result["examined"], 64)
        self.assertEqual(result["removed"], 16)
        self.assertEqual(sum(self.store.get(duty_id) is None for duty_id in old_ids), 16)
        self.assertEqual(self.store.prune_terminal_obligations(now=now)["status"], "not-due")

        with mock.patch.object(obligations.time, "monotonic", return_value=0.0):
            next_pass = self.store.prune_terminal_obligations(now=now + 300)
        self.assertEqual(next_pass["removed"], 4)
        self.assertIsNotNone(self.store.get(pending["id"]))
        self.assertEqual(self.store.get(unknown["id"])["state"], "unknown")

        deadline_ids = []
        for index in range(2):
            duty_id = f"deadline-readiness-{index:02d}"
            duty = self.store.create(duty_id, "readiness-observation",
                                     {"consumer": {"pid": 2_000_000_100 + index,
                                                    "pid_start": "1"}}, {})
            self.store.update(duty["id"], state="complete", result="consumer-exited")
            record_path = self.store._record_path(duty["id"])
            persisted = json.loads(record_path.read_text(encoding="utf-8"))
            persisted["terminal_at"] = now - 2 * 86400
            record_path.write_text(json.dumps(persisted), encoding="utf-8")
            deadline_ids.append(duty_id)

        with mock.patch.object(obligations.time, "monotonic",
                               side_effect=(0.0, 0.0, 0.05)) as clock:
            deadline_pass = self.store.prune_terminal_obligations(
                now=now + 3600, time_budget=0.05)
        self.assertEqual(clock.call_count, 3)
        self.assertEqual(deadline_pass["examined"], 1)
        self.assertEqual(deadline_pass["removed"], 1)
        self.assertEqual(deadline_pass["cursor"], deadline_ids[0] + ".json")
        self.assertIsNone(self.store.get(deadline_ids[0]))
        self.assertIsNotNone(self.store.get(deadline_ids[1]))

        not_due = self.store.prune_terminal_obligations(now=now + 3601)
        self.assertEqual(not_due["status"], "not-due")
        self.assertEqual(not_due["cursor"], deadline_pass["cursor"])
        with mock.patch.object(obligations.time, "monotonic", return_value=0.0):
            resumed = self.store.prune_terminal_obligations(now=now + 3900)
        self.assertEqual(resumed["removed"], 1)
        self.assertIsNone(self.store.get(deadline_ids[1]))
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

    def notice_transfer(self, harness="codex", *, member_count=1, suffix="", referenced=False):
        import dispatch_pending_delivery as pending
        import dispatch_session_sweep as sweep
        import session_notice
        peer = peer_steward.peer_message
        sid = "notice-" + harness + suffix
        records, duties = [], []
        for index in range(member_count):
            with mock.patch.object(obligations, "ensure_normal_runner", return_value=False):
                record = _legacy_notice(harness, sid, key=f"notice:{sid}:{index}",
                    subject="completed", text="finished", jobs=self.jobs)
            duty = next(duty for duty in self.store.list(kinds={"session-notice"})
                        if duty["intent"]["identity"]["delivery_id"] == record["delivery_id"])
            records.append(pending.claim(self.root, sid, record["delivery_id"],
                claim_owner="retained-rewake:" + duty["id"], lease_seconds=30, jobs=self.jobs))
            duties.append(duty)
        recipient = {"harness": harness, "session_id": sid}
        producer = {"schema": "session-notice-runtime-v1", "duty_id": duties[0]["id"],
            "jobs": str(self.jobs), "recipient": recipient,
            "records": [{"duty_id": duty["id"], "storage_sid": sid,
                "delivery_id": record["delivery_id"], "receipt_digest": record["receipt_digest"]}
                for record, duty in zip(records, duties)]}
        refs = [value for record, duty in zip(records, duties)
                for value in (duty["id"], record["delivery_id"])]
        body = sweep.delivery_context([(self.root, [dict(record, **{sweep.STORAGE_KEY: sid,
            sweep.IDENTITY_KEY: pending.received_ack_identity(record, sid, record["delivery_id"])})
            for record in records])])
        with mock.patch.object(peer, "_reference_access_evidence_valid", return_value=referenced):
            _text, ref = peer.prepare_peer_message(body + ("\n" + "padding " * 400 if referenced else ""),
                {"harness": "runtime", "session_id": None, "name": "Hearting runtime"}, recipient,
                defer=True, refs=refs, producer_binding=producer)
        if referenced:
            self.assertIsNotNone(peer._read_pending(ref).get("body_ref"))
        return ref, recipient, records, duties

    def _native_first_write(self, ref, recipient, *, before=None):
        """Both actual transport boundaries; a native mock invokes before_add."""
        peer = peer_steward.peer_message
        writes = []
        if recipient["harness"] == "claude":
            reservation = peer.reserve_pending_herdr(ref, recipient)
            if before:
                before()
            if peer.begin_pending_write(reservation, recipient=recipient) is not None:
                writes.append(ref)
        else:
            def native(*args, **kwargs):
                if not kwargs["allow_add"]:
                    return {"status": "pending"}
                if before:
                    before()
                kwargs["before_add"]()
                writes.append(kwargs["message"])
                return {"status": "pending"}
            with mock.patch("codex_queue_dispatch.resolve_queue_socket", return_value=self.root / "native.sock"), \
                 mock.patch("codex_queue_delivery.send_at_least_once", side_effect=native):
                peer.deliver_pending_codex(ref)
        return writes

    def test_route_free_first_write_checks_all_members_at_both_native_boundaries(self):
        import dispatch_pending_delivery as pending
        for harness in ("claude", "codex"):
            for count in (1, 4, 8):
                for index in range(count):
                    for mutation in ("owner", "identity", "fence", "expired-unrenewed", "renew", "terminal"):
                        with self.subTest(harness=harness, count=count, index=index, mutation=mutation):
                            suffix = f"-{count}-{index}-{mutation}"
                            ref, recipient, records, duties = self.notice_transfer(
                                harness, member_count=count, suffix=suffix)
                            record, sid = records[index], recipient["session_id"]
                            did = record["delivery_id"]
                            def change():
                                if mutation == "owner":
                                    pending.release_claim(self.root, sid, did, claim_owner=record["claim_owner"])
                                    pending.claim(self.root, sid, did, claim_owner="other-carrier", lease_seconds=30,
                                                  jobs=self.jobs, retry_context="new-carrier")
                                elif mutation == "fence":
                                    pending.prepare_submission_fence(self.root, sid, did,
                                        claim_owner=record["claim_owner"],
                                        expected_identity=pending.submission_fence_identity(record, sid))
                                elif mutation == "terminal":
                                    pending.ack_received(self.root, sid, did,
                                        expected_identity=pending.received_ack_identity(record, sid, did),
                                        acked_by="other-exact-receipt")
                                else:
                                    path = pending.record_path(self.root, sid, did)
                                    raw = json.loads(path.read_text())
                                    if mutation == "identity":
                                        raw["route_node"] = "replacement-node"
                                    else:
                                        raw["claim_deadline_ns"] = time.monotonic_ns() - 1
                                    path.write_text(json.dumps(raw))
                            # Refusing/no-op renewal cannot authorize an expired write.
                            renew = pending.renew_unsubmitted_claim
                            with mock.patch.object(pending, "renew_unsubmitted_claim",
                                    side_effect=(lambda *a, **k: record) if mutation == "expired-unrenewed" else renew) as renewal:
                                writes = self._native_first_write(ref, recipient, before=change)
                            self.assertEqual(len(writes), int(mutation == "renew"))
                            fresh = pending.read(self.root, sid, did)
                            if mutation == "renew":
                                self.assertEqual(renewal.call_count, 1)
                                self.assertGreater(fresh["claim_deadline_ns"], time.monotonic_ns())
                                self.assertEqual(fresh["attempts"], record["attempts"])
                            if mutation == "owner":
                                self.assertEqual(fresh["claim_owner"], "other-carrier")
                            if mutation != "terminal":
                                self.assertNotEqual(fresh["state"], "acked")

    def test_original_body_identity_is_used_by_both_receipt_paths_for_all_fields(self):
        import dispatch_pending_delivery as pending
        peer = peer_steward.peer_message
        changes = {"delivery_id": "delivery-replacement", "recipient_digest": "a" * 64,
            "recipient_kind": "claude-parent-runtime", "session_generation": "replacement",
            "session_generation_supported": "1", "attempt_ids": ["att-replacement"],
            "parent_attempt_id": "att-replacement", "route_id": "rt-replacement",
            "route_node": "replacement-node", "row_revisions": {"att-replacement": "new"},
            "lineage": ["replacement"]}
        for producer in ("notice", "registered"):
            for referenced in (False, True):
                for field, value in changes.items():
                    with self.subTest(producer=producer, referenced=referenced, field=field):
                        suffix = f"-{producer}-{int(referenced)}-{field}"
                        if producer == "notice":
                            ref, recipient, records, _ = self.notice_transfer(
                                member_count=4, suffix=suffix, referenced=referenced)
                        else:
                            # A long real registered group uses the same verified payload reader.
                            with mock.patch.object(peer, "_reference_access_evidence_valid", return_value=referenced):
                                ref, recipient, records, _ = self.registered_transfer(
                                    member_count=24 if referenced else 2, suffix=suffix)
                            self.assertEqual(bool(peer._read_pending(ref).get("body_ref")), referenced)
                        did, sid = records[0]["delivery_id"], recipient["session_id"]
                        wire = peer._read_pending(ref)["text"]
                        path = pending.record_path(self.root, sid, did)
                        raw = json.loads(path.read_text()); raw[field] = value
                        path.write_text(json.dumps(raw))
                        self.assertEqual(peer.receive_peer_message(wire, recipient), 0)
                        if field in {"delivery_id", "recipient_digest", "attempt_ids"}:
                            # These address fields also bind the existing store sidecar.
                            with self.assertRaises(pending.PendingDeliveryError):
                                pending.read(self.root, sid, did)
                            fresh = json.loads(path.read_text())
                        else:
                            fresh = pending.read(self.root, sid, did)
                        self.assertEqual(fresh["receipt_digest"], records[0]["receipt_digest"])
                        self.assertNotEqual(fresh["state"], "acked")
                        # One replacement cannot discard the other exact original receipts.
                        self.assertTrue(all(pending.read(self.root, sid, member["delivery_id"])["state"] == "acked"
                                            for member in records[1:]))

    def test_exact_original_receipt_survives_lease_owner_lineage_and_ack_write_failure(self):
        import dispatch_pending_delivery as pending
        import session_tidy
        peer = peer_steward.peer_message
        for producer in ("notice", "registered"):
            for harness in ("claude", "codex"):
                for referenced in (False, True):
                    with self.subTest(producer=producer, harness=harness, referenced=referenced):
                        suffix = f"-receipt-{producer}-{harness}-{int(referenced)}"
                        if producer == "notice":
                            ref, recipient, records, _ = self.notice_transfer(
                                harness, member_count=4, suffix=suffix, referenced=referenced)
                        else:
                            with mock.patch.object(peer, "_reference_access_evidence_valid", return_value=referenced):
                                ref, recipient, records, _ = self.registered_transfer(
                                    harness, member_count=24 if referenced else 2, suffix=suffix)
                        sid = recipient["session_id"]
                        wire = peer._read_pending(ref)["text"]
                        seal = peer._transfer_path(ref).read_bytes()
                        session_tidy._append_ledger_entry(session_tidy.Seat("pane", suffix),
                            dict(event="succession", source="tidy-clear", harness=harness,
                                 sid=sid + "-successor", **{"from": {"harness": harness, "sid": sid}}, ts=1))
                        for record in records:
                            path = pending.record_path(self.root, sid, record["delivery_id"])
                            raw = json.loads(path.read_text())
                            raw.update(claim_owner="new-holder", claim_deadline_ns=time.monotonic_ns() - 1)
                            path.write_text(json.dumps(raw))
                        # A real unavailable global graph is irrelevant to a historical exact receipt.
                        ledger = session_tidy.state_root() / "sessions" / (suffix + ".jsonl")
                        ledger.parent.mkdir(parents=True, exist_ok=True); ledger.write_text("{")
                        with mock.patch.object(pending, "ack_received", side_effect=OSError("ACK write unavailable")):
                            self.assertEqual(peer.receive_peer_message(wire, recipient), 0)
                        received = peer._read_pending(ref)
                        self.assertEqual(received["state"], "received")
                        self.assertEqual(received["text"], wire)
                        self.assertTrue(all(pending.read(self.root, sid, r["delivery_id"])["state"] == "claimed"
                                            for r in records))
                        with mock.patch.object(peer, "prepare_peer_message", side_effect=AssertionError("resend")):
                            self.assertTrue(peer._ack_registered_delivery(received))
                        self.assertTrue(all(pending.read(self.root, sid, r["delivery_id"])["state"] == "acked"
                                            for r in records))
                        self.assertEqual(peer._transfer_path(ref).read_bytes(), seal)
                        ledger.unlink()  # Own isolated error fixture only.

    def test_registered_24_member_body_rechecks_each_member_in_actual_native_callback(self):
        import dispatch_pending_delivery as pending
        peer = peer_steward.peer_message
        for index in range(24):
            with self.subTest(index=index):
                ref, recipient, records, _ = self.registered_transfer(member_count=24, suffix=f"-native-{index}")
                record, sid = records[index], recipient["session_id"]
                def change():
                    path = pending.record_path(self.root, sid, record["delivery_id"])
                    raw = json.loads(path.read_text()); raw["route_node"] = "replacement-node"
                    path.write_text(json.dumps(raw))
                self.assertEqual(self._native_first_write(ref, recipient, before=change), [])
                self.assertEqual(peer._read_pending(ref)["state"], "pending")
        ref, recipient, records, _ = self.registered_transfer(member_count=24, suffix="-native-positive")
        self.assertEqual(len(self._native_first_write(ref, recipient)), 1)
        self.assertEqual(peer._read_pending(ref)["state"], "queued")
        self.assertEqual(self._native_first_write(ref, recipient), [])
        wire = peer._read_pending(ref)["text"]
        self.assertEqual(peer.receive_peer_message(wire, recipient), 0)
        self.assertTrue(all(pending.read(self.root, recipient["session_id"], r["delivery_id"])["state"] == "acked"
                            for r in records))

    def test_route_free_runner_releases_only_original_unsubmitted_group_claims(self):
        import dispatch_pending_delivery as pending
        import dispatch_session_sweep as sweep
        import session_notice
        from types import SimpleNamespace
        for index in range(4):
            for mutation in ("identity", "owner", "fence", "lease", "terminal"):
                with self.subTest(index=index, mutation=mutation):
                    sid = f"runner-{index}-{mutation}"
                    with mock.patch.object(obligations, "ensure_normal_runner", return_value=False):
                        records = [_legacy_notice("claude", sid, key=f"runner:{sid}:{n}",
                                   subject="done", text="completed", jobs=self.jobs) for n in range(4)]
                    duties = [duty for duty in self.store.list(kinds={"session-notice"})
                              if duty["intent"]["identity"]["storage_sid"] == sid]
                    changed = records[index]["delivery_id"]
                    captured = {}
                    real_render = sweep.delivery_context
                    def render(groups):
                        record = pending.read(self.root, sid, changed)
                        if mutation == "owner":
                            pending.release_claim(self.root, sid, changed, claim_owner=record["claim_owner"])
                            pending.claim(self.root, sid, changed, claim_owner="other-carrier", lease_seconds=30,
                                          jobs=self.jobs, retry_context="new-owner")
                        elif mutation == "terminal":
                            pending.ack_received(self.root, sid, changed,
                                expected_identity=pending.received_ack_identity(record, sid, changed),
                                acked_by="already-exact")
                        elif mutation == "fence":
                            pending.prepare_submission_fence(self.root, sid, changed,
                                claim_owner=record["claim_owner"],
                                expected_identity=pending.submission_fence_identity(record, sid))
                        else:
                            path = pending.record_path(self.root, sid, changed)
                            raw = json.loads(path.read_text())
                            raw.update({"route_node": "replacement-node"} if mutation == "identity" else
                                       {"claim_deadline_ns": time.monotonic_ns() - 1})
                            path.write_text(json.dumps(raw))
                        path = pending.record_path(self.root, sid, changed)
                        captured["bytes"] = path.read_bytes()
                        return real_render(groups)
                    agents = {"result": {"agents": [{"agent": "claude", "pane_id": "w1:pNotice",
                              "agent_session": {"value": sid}}]}}
                    with mock.patch.object(peer_steward.subprocess, "run", return_value=SimpleNamespace(
                            returncode=0, stdout=json.dumps(agents))), \
                         mock.patch.object(peer_steward, "_resolve_target", return_value=("claude", sid)), \
                         mock.patch.object(sweep, "delivery_context", side_effect=render), \
                         mock.patch.object(peer_steward, "cmd_prompt") as prompt:
                        peer_steward._resume_session_notice_obligations(duties, self.store)
                    prompt.assert_not_called()
                    self.assertEqual(pending.record_path(self.root, sid, changed).read_bytes(), captured["bytes"])
                    self.assertTrue(all(pending.read(self.root, sid, r["delivery_id"])["state"] == "pending"
                                        for r in records if r["delivery_id"] != changed))

    def test_notice_group_recovers_at_current_holder_on_the_next_normal_pass(self):
        import dispatch_pending_delivery as pending
        import dispatch_session_sweep as sweep
        import session_notice
        import session_tidy as tidy
        from types import SimpleNamespace
        peer = peer_steward.peer_message
        sid, successor = "next-pass-original", "next-pass-current"
        with mock.patch.object(obligations, "ensure_normal_runner", return_value=False):
            records = [_legacy_notice("claude", sid, key=f"recovery:{n}", subject="done",
                       text="finished", jobs=self.jobs) for n in range(4)]
        duties = self.store.list(kinds={"session-notice"})
        real_render = sweep.delivery_context
        def redirect(groups):
            tidy._append_ledger_entry(tidy.Seat("pane", "next-pass"),
                dict(event="succession", source="tidy-clear", harness="claude", sid=successor,
                     **{"from": {"harness": "claude", "sid": sid}}, ts=1))
            return real_render(groups)
        def inventory(target):
            return SimpleNamespace(returncode=0, stdout=json.dumps({"result": {"agents": [{
                "agent": "claude", "pane_id": "w1:pRecovery", "agent_session": {"value": target}}]}}))
        with mock.patch.object(peer_steward.subprocess, "run", return_value=inventory(sid)), \
             mock.patch.object(peer_steward, "_resolve_target", return_value=("claude", sid)), \
             mock.patch.object(sweep, "delivery_context", side_effect=redirect), \
             mock.patch.object(peer_steward, "cmd_prompt") as prompt:
            peer_steward._resume_session_notice_obligations(duties, self.store)
        prompt.assert_not_called()
        sent = []
        def courier(args, *, expected_recipient, on_prepared, producer_binding):
            self.assertEqual(expected_recipient, ("claude", successor))
            wire, ref = peer.prepare_peer_message(Path(args.body_file).read_text(),
                {"harness": "runtime", "session_id": None, "name": "Hearting runtime"},
                {"harness": "claude", "session_id": successor}, defer=True,
                refs=args.ref, producer_binding=producer_binding)
            on_prepared(ref)
            sent.append(ref)
            self.assertEqual(peer.receive_peer_message(wire, {"harness": "claude", "session_id": successor}), 0)
            return 0
        with mock.patch.object(peer_steward.subprocess, "run", return_value=inventory(successor)), \
             mock.patch.object(peer_steward, "_resolve_target", return_value=("claude", successor)), \
             mock.patch.object(peer_steward, "cmd_prompt", side_effect=courier):
            peer_steward._resume_session_notice_obligations(self.store.list(kinds={"session-notice"}), self.store)
        self.assertEqual(len(sent), 1)
        self.assertTrue(all(pending.read(self.root, sid, r["delivery_id"])["state"] == "acked" for r in records))
        self.assertFalse((self.root / "pending-delivery" / pending.recipient_digest(successor)).exists())

    def test_prepared_notice_group_recovers_after_later_handover_without_mutating_old_transfer(self):
        import dispatch_pending_delivery as pending
        import session_tidy as tidy
        from types import SimpleNamespace
        peer = peer_steward.peer_message
        os.environ["AGENT_PEER_LEDGER_ROOT"] = str(self.root)
        ref, recipient, records, duties = self.notice_transfer("claude", member_count=4, suffix="-retarget")
        sid, successor = recipient["session_id"], "retargeted-current-holder"
        seal = peer._transfer_path(ref).read_bytes()
        for record, duty in zip(records, duties):
            pending.mark_sent_ambiguous(self.root, sid, record["delivery_id"], claim_owner=record["claim_owner"])
            self.store.update(duty["id"], observation={"transfer_ref": ref})
        tidy._append_ledger_entry(tidy.Seat("pane", "prepared-retarget"),
            dict(event="succession", source="tidy-clear", harness="claude", sid=successor,
                 **{"from": {"harness": "claude", "sid": sid}}, ts=1))
        for record in records:
            path = pending.record_path(self.root, sid, record["delivery_id"])
            raw = json.loads(path.read_text()); raw["claim_deadline_ns"] = time.monotonic_ns() - 1
            path.write_text(json.dumps(raw))
        sent = []
        def courier(args, *, expected_recipient, on_prepared, producer_binding):
            self.assertEqual(expected_recipient, ("claude", successor))
            wire, new_ref = peer.prepare_peer_message(Path(args.body_file).read_text(),
                {"harness": "runtime", "session_id": None, "name": "Hearting runtime"},
                {"harness": "claude", "session_id": successor}, defer=True,
                refs=args.ref, producer_binding=producer_binding)
            on_prepared(new_ref); sent.append(new_ref)
            self.assertEqual(peer.receive_peer_message(wire, {"harness": "claude", "session_id": successor}), 0)
            return 0
        agents = {"result": {"agents": [{"agent": "claude", "pane_id": "w1:pRetarget",
                  "agent_session": {"value": successor}}]}}
        with mock.patch.object(peer_steward.subprocess, "run", return_value=SimpleNamespace(
                returncode=0, stdout=json.dumps(agents))), \
             mock.patch.object(peer_steward, "_resolve_target", return_value=("claude", successor)), \
             mock.patch.object(peer_steward, "cmd_prompt", side_effect=courier):
            peer_steward._resume_session_notice_obligations(self.store.list(kinds={"session-notice"}), self.store)
        self.assertEqual(len(sent), 1)
        self.assertNotEqual(sent[0], ref)
        self.assertEqual(peer._transfer_path(ref).read_bytes(), seal)
        self.assertEqual(peer._read_pending(ref)["state"], "pending")
        self.assertTrue(all(pending.read(self.root, sid, r["delivery_id"])["state"] == "acked" for r in records))

    def test_prepared_retarget_holds_group_for_changed_owner_fence_identity_or_live_lease(self):
        import dispatch_pending_delivery as pending
        import session_tidy as tidy
        from types import SimpleNamespace
        peer = peer_steward.peer_message
        os.environ["AGENT_PEER_LEDGER_ROOT"] = str(self.root)
        for mutation in ("owner", "fence", "identity", "queued", "unverified", "live-lease"):
            with self.subTest(mutation=mutation):
                ref, recipient, records, duties = self.notice_transfer("claude", member_count=4,
                                                                      suffix="-held-" + mutation)
                sid, successor = recipient["session_id"], "current-" + mutation
                last = max(records, key=lambda value: value["delivery_id"])
                for record, duty in zip(records, duties):
                    if mutation == "fence" and record["delivery_id"] == last["delivery_id"]:
                        # Fence the add while live, then retain it after expiry.
                        pending.prepare_submission_fence(self.root, sid, last["delivery_id"],
                            claim_owner=last["claim_owner"],
                            expected_identity=pending.submission_fence_identity(last, sid))
                    pending.mark_sent_ambiguous(self.root, sid, record["delivery_id"], claim_owner=record["claim_owner"])
                    self.store.update(duty["id"], observation={"transfer_ref": ref})
                    path = pending.record_path(self.root, sid, record["delivery_id"])
                    raw = json.loads(path.read_text()); raw["claim_deadline_ns"] = time.monotonic_ns() - 1
                    path.write_text(json.dumps(raw))
                tidy._append_ledger_entry(tidy.Seat("pane", "held-" + mutation),
                    dict(event="succession", source="tidy-clear", harness="claude", sid=successor,
                         **{"from": {"harness": "claude", "sid": sid}}, ts=1))
                last = max(records, key=lambda value: value["delivery_id"])
                path = pending.record_path(self.root, sid, last["delivery_id"])
                if mutation in {"queued", "unverified"}:
                    peer._save_pending(dict(peer._read_pending(ref), state=mutation))
                elif mutation == "fence":
                    raw = json.loads(path.read_text()); raw["state"] = "claimed"
                    path.write_text(json.dumps(raw))
                else:
                    raw = json.loads(path.read_text())
                    raw.update({"claim_owner": "other-carrier"} if mutation == "owner" else
                               {"route_node": "replacement-node"} if mutation == "identity" else
                               {"claim_deadline_ns": time.monotonic_ns() + 30_000_000_000})
                    path.write_text(json.dumps(raw))
                protected = path.read_bytes()
                agents = {"result": {"agents": [{"agent": "claude", "pane_id": "w1:pHeld",
                          "agent_session": {"value": successor}}]}}
                with mock.patch.object(peer_steward.subprocess, "run", return_value=SimpleNamespace(
                        returncode=0, stdout=json.dumps(agents))), \
                     mock.patch.object(peer_steward, "_resolve_target", return_value=("claude", successor)), \
                     mock.patch.object(peer_steward, "cmd_prompt") as prompt:
                    peer_steward._resume_session_notice_obligations([self.store.get(d["id"]) for d in duties], self.store)
                prompt.assert_not_called()
                self.assertEqual(path.read_bytes(), protected)
                if mutation == "live-lease":
                    # Earlier claims returned safely to pending can recover normally too.
                    raw = json.loads(path.read_text()); raw["claim_deadline_ns"] = time.monotonic_ns() - 1
                    path.write_text(json.dumps(raw))
                    sent = []
                    def courier(args, *, expected_recipient, on_prepared, producer_binding):
                        wire, new_ref = peer.prepare_peer_message(Path(args.body_file).read_text(),
                            {"harness": "runtime", "session_id": None, "name": "Hearting runtime"},
                            {"harness": "claude", "session_id": successor}, defer=True,
                            refs=args.ref, producer_binding=producer_binding)
                        on_prepared(new_ref); sent.append(new_ref)
                        self.assertEqual(peer.receive_peer_message(wire, {"harness": "claude", "session_id": successor}), 0)
                        return 0
                    with mock.patch.object(peer_steward.subprocess, "run", return_value=SimpleNamespace(
                            returncode=0, stdout=json.dumps(agents))), \
                         mock.patch.object(peer_steward, "_resolve_target", return_value=("claude", successor)), \
                         mock.patch.object(peer_steward, "cmd_prompt", side_effect=courier), \
                         mock.patch.object(pending.time, "time_ns", return_value=time.time_ns() + 1_000_000_000):
                        peer_steward._resume_session_notice_obligations([self.store.get(d["id"]) for d in duties], self.store)
                    self.assertEqual(len(sent), 1)
                    self.assertTrue(all(pending.read(self.root, sid, r["delivery_id"])["state"] == "acked" for r in records))

    def test_native_history_receipts_use_the_same_original_identity_for_both_producers(self):
        import dispatch_pending_delivery as pending
        from types import SimpleNamespace
        peer = peer_steward.peer_message
        for producer in ("notice", "registered"):
            for harness in ("codex", "opencode"):
                for replacement in (False, True):
                    with self.subTest(producer=producer, harness=harness, replacement=replacement):
                        suffix = f"-native-receipt-{producer}-{harness}-{int(replacement)}"
                        if producer == "notice":
                            ref, recipient, records, _ = self.notice_transfer(harness, member_count=4, suffix=suffix)
                        else:
                            ref, recipient, records, _ = self.registered_transfer(harness, suffix=suffix)
                        wire, sid = peer._read_pending(ref)["text"], recipient["session_id"]
                        if harness == "codex":
                            self.assertEqual(len(self._native_first_write(ref, recipient)), 1)
                        else:
                            self.assertEqual(peer.cmd_pending(SimpleNamespace(to_harness=harness, to_session_id=sid,
                                queued=ref, message_id="native-message-" + ref)), 0)
                        path = pending.record_path(self.root, sid, records[0]["delivery_id"])
                        raw = json.loads(path.read_text())
                        if replacement:
                            raw["route_node"] = "replacement-node"
                        else:
                            raw.update(claim_owner="other-holder", claim_deadline_ns=time.monotonic_ns() - 1)
                        path.write_text(json.dumps(raw))
                        if harness == "codex":
                            def consumed(*args, **kwargs):
                                self.assertFalse(kwargs["allow_add"])
                                self.assertTrue(kwargs["require_message_match"])
                                self.assertEqual(kwargs["message"], wire)
                                self.assertEqual(kwargs["client_message_id"], "delivery-peer-" + ref)
                                return {"status": "consumed"}
                            with mock.patch("codex_queue_dispatch.resolve_queue_socket", return_value=self.root / "native.sock"), \
                                 mock.patch("codex_queue_delivery.send_at_least_once", side_effect=consumed):
                                self.assertEqual(peer.deliver_pending_codex(ref)["status"], "received")
                        else:
                            with mock.patch.object(peer, "retry_receiver_idle"):
                                self.assertEqual(peer.cmd_pending(SimpleNamespace(to_harness=harness, to_session_id=sid,
                                    received=ref, message_id="native-message-" + ref)), 0)
                        self.assertEqual(peer._read_pending(ref)["receipt"], "exact-native-history")
                        self.assertEqual(pending.read(self.root, sid, records[0]["delivery_id"])["state"] == "acked",
                                         not replacement)
                        self.assertTrue(all(pending.read(self.root, sid, r["delivery_id"])["state"] == "acked"
                                            for r in records[1:]))

    def test_receipt_cas_race_terminal_and_missing_original_proof_preserve_responsibility(self):
        import dispatch_pending_delivery as pending
        peer = peer_steward.peer_message
        for producer in ("notice", "registered"):
            for mutation in ("identity", "terminal", "missing-proof"):
                with self.subTest(producer=producer, mutation=mutation):
                    suffix = "-cas-" + producer + mutation
                    if producer == "notice":
                        ref, recipient, records, duties = self.notice_transfer(member_count=1, suffix=suffix)
                    else:
                        ref, recipient, records, _ = self.registered_transfer(suffix=suffix)
                        declared = peer._read_pending(ref)["dispatch_notices"]
                        duties = [self.store.get(item["duty_id"]) for item in declared]
                    sid, wire = recipient["session_id"], peer._read_pending(ref)["text"]
                    for duty in duties:
                        self.store.update(duty["id"], observation={"transfer_ref": ref})
                    real_ack = pending.ack_received
                    protected = {}
                    def race(root, storage, delivery_id, **kwargs):
                        self.assertEqual(kwargs["expected_identity"], pending.received_ack_identity(
                            records[0], sid, records[0]["delivery_id"]))
                        path = pending.record_path(root, storage, delivery_id)
                        raw = json.loads(path.read_text())
                        raw.update({"route_node": "replacement-node"} if mutation == "identity" else {"state": "expired"})
                        path.write_text(json.dumps(raw)); protected["path"] = path; protected["bytes"] = path.read_bytes()
                        return real_ack(root, storage, delivery_id, **kwargs)
                    if mutation == "missing-proof":
                        with mock.patch.object(pending, "ack_received", side_effect=OSError("interrupted")):
                            self.assertEqual(peer.receive_peer_message(wire, recipient), 0)
                        peer._save_pending(dict(peer._read_pending(ref), text=None))
                        self.assertFalse(peer._ack_registered_delivery(peer._read_pending(ref)))
                    else:
                        def first_only(root, storage, delivery_id, **kwargs):
                            if delivery_id == records[0]["delivery_id"]:
                                return race(root, storage, delivery_id, **kwargs)
                            return real_ack(root, storage, delivery_id, **kwargs)
                        with mock.patch.object(pending, "ack_received", side_effect=first_only):
                            self.assertEqual(peer.receive_peer_message(wire, recipient), 0)
                        self.assertEqual(protected["path"].read_bytes(), protected["bytes"])
                    # The actual transport and retained owner keep this received ref, without new native I/O.
                    with mock.patch("codex_queue_delivery.send_at_least_once", side_effect=AssertionError("resend")), \
                         mock.patch.object(peer_steward, "cmd_prompt", side_effect=AssertionError("resend")):
                        self.assertEqual(peer.deliver_pending_codex(ref)["status"], "received")
                        if producer == "registered" and mutation != "terminal":
                            peer_steward._resume_registered_obligation(self.store.get(duties[0]["id"]), self.store)
                    if mutation != "terminal":
                        self.assertNotEqual(self.store.get(duties[0]["id"])["state"], "complete")

    def test_a_received_gate_does_not_close_a_later_terminal_completion(self):
        import dispatch_pending_delivery as pending
        from dispatch_recipient_receipt import receipt_marker
        peer = peer_steward.peer_message
        sid, records, duties, metas = self.registered_transfer("claude", prepare=False)
        final, duty, meta = records[0], duties[0], metas[0]
        owner = "retained-rewake:" + duty["id"]
        recipient = {"harness": "claude", "session_id": sid}
        gate_body = {"kind": "completion", "phase": "earlier-gate"}
        gate = pending.create(self.root, recipient_kind="claude-parent-runtime", recipient_key=sid,
            delivery_id="delivery-earlier-gate", session_generation="", session_generation_supported="0",
            attempt_ids=final["attempt_ids"], parent_attempt_id=final["parent_attempt_id"],
            route_id=final["route_id"], route_node=final["route_node"], receipt=gate_body,
            receipt_digest=pending._canonical_receipt_digest(gate_body), row_revisions=final["row_revisions"])
        pending.claim(self.root, sid, gate["delivery_id"], claim_owner=owner, lease_seconds=30, jobs=self.jobs)

        def transfer(record):
            wire, ref = peer.prepare_peer_message(receipt_marker(record, sid),
                {"harness": "claude", "session_id": "courier"}, recipient,
                defer=True, refs=[duty["id"]])
            peer.bind_dispatch_notice(ref, {"duty_id": duty["id"], "jobs": str(self.jobs),
                "attempt_id": meta["attempt_id"], "carrier": "claude-parent-runtime",
                "recipient_sid": sid, "storage_recipient": sid, "delivery_id": record["delivery_id"],
                "receipt_digest": record["receipt_digest"], "claim_owner": owner})
            return wire, ref

        wire, ref = transfer(gate)
        with mock.patch.object(pending, "ack_received", wraps=pending.ack_received) as ack:
            self.assertEqual(peer.receive_peer_message(wire, recipient), 0)
            message = peer._read_pending(ref)
            ack.assert_called_once()
            self.assertEqual(len(list(obligations._registered_delivery_bindings(
                message, roots=[self.root], received_only=True))), 1)
            self.assertTrue(obligations.acknowledge_registered_delivery(message, roots=[self.root]))
            self.assertEqual(self.store.get(duty["id"])["state"], "pending")
            self.assertTrue(obligations.registered_delivery_settled(message, roots=[self.root]))
            ack.reset_mock()
            for invalid in (
                {**message, "ref": "z" * 32},
                {**message, "to": {"harness": "claude", "session_id": "other"}},
                {**message, "to": {"harness": "codex", "session_id": sid}},
                {**message, "dispatch_notice": {**message["dispatch_notice"], "attempt_id": "att-other"}},
                {**message, "dispatch_notice": {**message["dispatch_notice"], "delivery_id": "delivery-missing"}},
                {**message, "dispatch_notice": {**message["dispatch_notice"], "storage_recipient": "other"}},
                {**message, "text": "replacement body"},
            ):
                with self.subTest(invalid=invalid):
                    self.assertFalse(obligations.acknowledge_registered_delivery(invalid, roots=[self.root]))
            self.assertFalse(obligations.acknowledge_registered_delivery(
                message, roots=[self.root / "foreign-artifacts"]))
            self.assertEqual(ack.call_count, 0)
            # Current send authority cannot invalidate an original historical receipt.
            with mock.patch("dispatch_seat_handover._effective", return_value=("successor", "claude")):
                self.assertTrue(obligations.acknowledge_registered_delivery(message, roots=[self.root]))
            self.assertEqual(ack.call_count, 0)

        pending.claim_ready_batch(self.root, sid, final["delivery_id"], claim_owner=owner,
                                  lease_seconds=30, jobs=self.jobs)
        final_wire, final_ref = transfer(final)
        real_update = obligations.ObligationStore.update
        failed_update = {"remaining": 1}
        def update_once(store, duty_id, **kwargs):
            if failed_update["remaining"] and kwargs.get("state") == "complete":
                failed_update["remaining"] -= 1
                raise OSError("simulated duty persistence interruption")
            return real_update(store, duty_id, **kwargs)
        with mock.patch.object(obligations.ObligationStore, "update", autospec=True, side_effect=update_once), \
             mock.patch.object(pending, "ack_received", wraps=pending.ack_received) as ack:
            self.assertEqual(peer.receive_peer_message(final_wire, recipient), 0)
            self.assertEqual(self.store.get(duty["id"])["state"], "pending")
            self.assertEqual(pending.read(self.root, sid, final["delivery_id"])["state"], "acked")
            self.assertTrue(obligations.acknowledge_registered_delivery(peer._read_pending(final_ref),
                                                                      roots=[self.root]))
            self.assertEqual(ack.call_count, 1)
            self.assertEqual(self.store.get(duty["id"])["state"], "complete")


    def test_batch_transfer_is_not_settled_when_one_declared_binding_is_invalid(self):
        terminal = (None, None, None, {"state": "acked"}, None, "sid", "delivery-a")
        message = {"state": "received", "receipt": "exact-peer-ref",
                   "dispatch_notices": [{"delivery_id": "delivery-a"},
                                        {"delivery_id": "delivery-b"}]}
        with mock.patch.object(obligations, "_registered_delivery_bindings",
                               return_value=iter([terminal])):
            self.assertFalse(obligations.registered_delivery_settled(message))

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

    def test_registered_completion_duty_uses_current_parent_when_caller_sid_is_stale(self):
        from types import SimpleNamespace
        carrier = "fixture-parent-runtime"
        jobs = self.root / "dispatch" / "jobs.log"
        row = SimpleNamespace(status="running", metadata={
            "parent_completion_delivery": carrier})
        with mock.patch("dispatch_completion_join.current_attempt_row", return_value=row), \
             mock.patch("dispatch_seat_handover.effective_parent", return_value="sid-successor"), \
             mock.patch("harness_capabilities.parent_completion",
                        return_value={"carrier": carrier}), \
             mock.patch.object(obligations, "ensure_normal_runner"):
            self.assertTrue(obligations.retain_registered_completion(
                jobs, "att-stale-caller", "sid-original", carrier))
        duty = obligations.ObligationStore(jobs.parent).list(kinds={"registered-batch"})[0]
        self.assertEqual(duty["intent"]["identity"]["session_id"], "sid-successor")

    def test_notice_runtime_binding_carries_per_record_original_storage_identity(self):
        recipient = {"harness": "claude", "session_id": "sid-current"}
        duty = "session-notice-" + "a" * 32
        delivery = "delivery-original"
        producer = {"schema": "session-notice-runtime-v1", "duty_id": duty,
            "jobs": str(self.jobs.resolve()), "records": [{"duty_id": duty,
                "storage_sid": "sid-original", "delivery_id": delivery,
                "receipt_digest": "b" * 64}], "recipient": recipient}
        sender = {"harness": "runtime", "session_id": None, "name": "Hearting runtime"}
        refs = [duty, delivery]
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "notice_producer_test_peer_message", _HERE / "peer-message.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertTrue(module._valid_session_notice_producer_binding(
            producer, sender, recipient, refs))
        self.assertFalse(module._valid_session_notice_producer_binding(
            producer, sender, recipient, [duty]))

    def test_runner_recovers_legacy_notice_and_wakes_exact_lineage_pane(self):
        from types import SimpleNamespace
        import dispatch_pending_delivery as pending
        import session_notice
        env = {"HOME": str(self.root / "home"),
               "XDG_STATE_HOME": str(self.root / "xdg-state"),
               "AGENT_DISPATCH_JOBS": str(self.jobs)}
        with mock.patch.dict(os.environ, env), \
             mock.patch.object(obligations, "ensure_normal_runner", return_value=False):
            record = _legacy_notice("claude", "sid-notice", key="run:legacy",
                subject="compute run", text="finished", jobs=self.jobs)
            store = obligations.ObligationStore(self.jobs.parent)
            sessions = self.root / "xdg-state" / "hearting" / "session-tidy" / "sessions"
            sessions.mkdir(parents=True, exist_ok=True)
            (sessions / "known-seat.jsonl").write_text(
                json.dumps({"event": "start", "harness": "claude", "sid": "sid-notice"}) + "\n")
            (self.root / "jobs.log").write_text(
                "2026-10-24T00:00:00Z\topen\t/w\t/w\tslug\t"
                "attempt_id=att-known,parent_sid=sid-notice,parent_completion_delivery=claude-parent-runtime,"
                "dispatch_depth=1,worker_type=owner,route_id=rt-known,route_hash=sha256:known\n")
            for path in store.root.glob("session-notice-*.json"):
                path.unlink()
            peer_steward._discover_session_notice_duties(store)
            duties = store.list(kinds={"session-notice"})
            sent = []
            def courier(args, *, expected_recipient, on_prepared, producer_binding=None):
                body = Path(args.body_file).read_text(encoding="utf-8")
                sent.append((body, expected_recipient, producer_binding, args.ref))
                on_prepared("a" * 32)
                return 0
            inventory = {"result": {"agents": [{"agent": "claude",
                "agent_session": {"value": "sid-notice"}, "pane_id": "pane-notice"}]}}
            with mock.patch.object(peer_steward, "_herdr_argv", return_value=["herdr"]), \
                 mock.patch.object(peer_steward.subprocess, "run",
                    return_value=SimpleNamespace(returncode=0, stdout=json.dumps(inventory))), \
                 mock.patch.object(peer_steward, "_resolve_target",
                    return_value=("claude", "sid-notice", "notice")), \
                 mock.patch.object(peer_steward, "cmd_prompt", side_effect=courier):
                peer_steward._resume_session_notice_obligations(duties, store)
            stored = pending.read(self.jobs.parent, "sid-notice", record["delivery_id"])
        self.assertEqual(len(duties), 1)
        self.assertEqual(len(sent), 1)
        body, target, producer, refs = sent[0]
        self.assertIn("compute run: finished", body)
        self.assertEqual(target, ("claude", "sid-notice"))
        self.assertEqual(producer["schema"], "session-notice-runtime-v1")
        self.assertEqual(producer["records"][0]["storage_sid"], "sid-notice")
        self.assertEqual(refs, [duties[0]["id"], record["delivery_id"]])
        self.assertEqual(stored["state"], "sent-ambiguous")

    def test_runner_preserves_notice_when_herdr_pane_id_is_missing_or_invalid(self):
        from types import SimpleNamespace
        import dispatch_pending_delivery as pending
        import session_notice

        env = {"HOME": str(self.root / "home"),
               "XDG_STATE_HOME": str(self.root / "xdg-state"),
               "AGENT_DISPATCH_JOBS": str(self.jobs)}
        with mock.patch.dict(os.environ, env), \
             mock.patch.object(obligations, "ensure_normal_runner", return_value=False):
            record = _legacy_notice("claude", "sid-notice", key="run:malformed-pane",
                subject="compute run", text="finished", jobs=self.jobs)
            store = obligations.ObligationStore(self.jobs.parent)
            sessions = self.root / "xdg-state" / "hearting" / "session-tidy" / "sessions"
            sessions.mkdir(parents=True, exist_ok=True)
            (sessions / "known-seat.jsonl").write_text(
                json.dumps({"event": "start", "harness": "claude", "sid": "sid-notice"}) + "\n")
            self.jobs.write_text(
                "2026-10-24T00:00:00Z\topen\t/w\t/w\tslug\t"
                "attempt_id=att-known,parent_sid=sid-notice,parent_completion_delivery=claude-parent-runtime,"
                "dispatch_depth=1,worker_type=owner,route_id=rt-known,route_hash=sha256:known\n")
            peer_steward._discover_session_notice_duties(store)
            duties = store.list(kinds={"session-notice"})
            cases = [("missing", None), ("none", None), ("integer", 9),
                     ("list", []), ("empty", ""), ("placeholder", "-")]
            for label, pane in cases:
                with self.subTest(pane_id=label):
                    agent = {"agent": "claude", "agent_session": {"value": "sid-notice"}}
                    if label != "missing":
                        agent["pane_id"] = pane
                    inventory = {"result": {"agents": [agent]}}
                    with mock.patch.object(peer_steward, "_herdr_argv", return_value=["herdr"]), \
                         mock.patch.object(peer_steward.subprocess, "run", return_value=SimpleNamespace(
                             stdout=json.dumps(inventory), returncode=0)), \
                         mock.patch.object(peer_steward, "_resolve_target") as resolve:
                        peer_steward._resume_session_notice_obligations(duties, store)
                    resolve.assert_not_called()
                    duty = store.get(duties[0]["id"])
                    self.assertEqual(duty["state"], "pending")
                    self.assertEqual(duty["observation"]["reason"], "recipient-unavailable")
                    self.assertEqual(duty["observation"]["recipient_sid"], "sid-notice")
                    stored = pending.read(self.jobs.parent, "sid-notice", record["delivery_id"])
                    self.assertEqual(stored["state"], "pending")

    def test_notice_runner_rotates_past_blocked_recipient_groups(self):
        from types import SimpleNamespace
        import dispatch_pending_delivery as pending
        import session_notice

        cases = ("pane-unavailable", "invalid-pane", "target-changed",
                 "queued", "unverified", "pending")
        for case in cases:
            with self.subTest(blocker=case):
                case_root = self.root / ("notice-fairness-" + case)
                case_root.mkdir()
                jobs = case_root / "jobs.log"
                jobs.touch()
                env = {
                    "HOME": str(case_root / "home"),
                    "XDG_STATE_HOME": str(case_root / "state"),
                    "XDG_DATA_HOME": str(case_root / "data"),
                    "XDG_CONFIG_HOME": str(case_root / "config"),
                    "XDG_CACHE_HOME": str(case_root / "cache"),
                    "HARNESS_STATE_ROOT": str(case_root / "harness"),
                    "AGENT_PEER_LEDGER_ROOT": str(case_root / "peer-ledger"),
                    "AGENT_DISPATCH_JOBS": str(jobs),
                }
                with mock.patch.dict(os.environ, env), \
                     mock.patch.object(obligations, "ensure_normal_runner", return_value=False):
                    front_record = _legacy_notice(
                        "claude", "sid-a-blocked", key="notice:" + case + ":front",
                        subject="front", text="blocked", jobs=jobs)
                    back_record = _legacy_notice(
                        "claude", "sid-z-live", key="notice:" + case + ":back",
                        subject="back", text="deliver", jobs=jobs)
                    self.assertIsNotNone(front_record)
                    self.assertIsNotNone(back_record)
                    store = obligations.ObligationStore(case_root)
                    duties = store.list(kinds={"session-notice"})
                    by_sid = {duty["intent"]["identity"]["storage_sid"]: duty
                              for duty in duties}
                    front_duty = by_sid["sid-a-blocked"]
                    back_duty = by_sid["sid-z-live"]
                    transfer_ref = "front-transfer-" + case
                    transfer_state = {"queued": "queued", "unverified": "unverified",
                                      "pending": "pending"}.get(case)
                    if transfer_state:
                        store.update(front_duty["id"], observation={
                            "transfer_ref": transfer_ref,
                            "storage_sid": "sid-a-blocked",
                            "delivery_id": front_record["delivery_id"],
                            "recipient_sid": "sid-a-blocked",
                        })

                    from dispatch_recipient_receipt import receipt_marker
                    peer = peer_steward.peer_message
                    owner = "retained-rewake:" + front_duty["id"]
                    front_claim = pending.claim(case_root, "sid-a-blocked", front_record["delivery_id"],
                        claim_owner=owner, lease_seconds=30, jobs=jobs)
                    recipient = {"harness": "claude", "session_id": "sid-a-blocked"}
                    producer = {"schema": "session-notice-runtime-v1", "duty_id": front_duty["id"],
                        "jobs": str(jobs), "recipient": recipient, "records": [{
                            "duty_id": front_duty["id"], "storage_sid": "sid-a-blocked",
                            "delivery_id": front_record["delivery_id"],
                            "receipt_digest": front_record["receipt_digest"]}]}
                    _wire, actual_ref = peer.prepare_peer_message(receipt_marker(front_claim, "sid-a-blocked"),
                        {"harness": "runtime", "session_id": None, "name": "Hearting runtime"}, recipient,
                        defer=True, refs=[front_duty["id"], front_record["delivery_id"]], producer_binding=producer)
                    pending.release_claim(case_root, "sid-a-blocked", front_record["delivery_id"], claim_owner=owner)
                    front_transfer = dict(peer._read_pending(actual_ref), state=transfer_state)
                    if transfer_state:
                        transfer_ref = actual_ref
                        store.update(front_duty["id"], observation={
                            "transfer_ref": transfer_ref, "storage_sid": "sid-a-blocked",
                            "delivery_id": front_record["delivery_id"], "recipient_sid": "sid-a-blocked"})
                    transfer_reads = []

                    def read_transfer(ref, **_kwargs):
                        if ref == transfer_ref:
                            transfer_reads.append(ref)
                            return dict(front_transfer)
                        return None

                    agents = [{"agent": "claude", "agent_session": {"value": "sid-z-live"},
                               "pane_id": "pane-z-live"}]
                    if case != "pane-unavailable":
                        front_pane = "-" if case == "invalid-pane" else "pane-a-blocked"
                        agents.insert(0, {"agent": "claude",
                                          "agent_session": {"value": "sid-a-blocked"},
                                          "pane_id": front_pane})
                    inventory = {"result": {"agents": agents}}

                    def resolve(pane):
                        if pane == "pane-a-blocked" and case == "target-changed":
                            return "claude", "sid-changed", "target-changed"
                        return "claude", {"pane-a-blocked": "sid-a-blocked",
                                           "pane-z-live": "sid-z-live"}.get(pane), "current"

                    sent = []

                    def courier(_args, *, expected_recipient, on_prepared,
                                producer_binding=None):
                        sent.append((expected_recipient, producer_binding))
                        on_prepared("back-transfer-" + case)
                        return 0

                    selection = {"state_root": str(case_root / "stale-root"),
                                 "runner_incarnation": "old-runner",
                                 "last_group": ("claude", "sid-z-live")}
                    with mock.patch.object(peer_steward, "_herdr_argv", return_value=["herdr"]), \
                         mock.patch.object(peer_steward.subprocess, "run", return_value=SimpleNamespace(
                             stdout=json.dumps(inventory), returncode=0)), \
                         mock.patch.object(peer_steward, "_resolve_target", side_effect=resolve), \
                         mock.patch.object(peer_steward.peer_message, "_read_pending",
                                           side_effect=read_transfer), \
                         mock.patch.object(peer_steward, "_schedule_message_obligation") as schedule, \
                         mock.patch.object(peer_steward, "cmd_prompt", side_effect=courier):
                        for _pass in range(3):
                            current = store.list(kinds={"session-notice"})
                            peer_steward._reconcile_obligations(
                                store, current, notice_selection=selection,
                                runner_incarnation="runner-" + case)

                    self.assertEqual([item[0] for item in sent],
                                     [("claude", "sid-z-live")])
                    self.assertEqual(sent[0][1]["recipient"],
                                     {"harness": "claude", "session_id": "sid-z-live"})
                    self.assertEqual(selection["state_root"], str(case_root.resolve()))
                    self.assertEqual(selection["runner_incarnation"], "runner-" + case)
                    self.assertEqual(selection["last_group"], ("claude", "sid-a-blocked"))
                    self.assertEqual(store.get(front_duty["id"])["state"], "pending")
                    self.assertEqual(store.get(back_duty["id"])["observation"]["transfer_ref"],
                                     "back-transfer-" + case)
                    original = pending.read(case_root, "sid-a-blocked",
                                            front_record["delivery_id"])
                    self.assertEqual(original["state"], "pending")
                    self.assertEqual(original["receipt_digest"], front_record["receipt_digest"])
                    self.assertIsNone(pending.read(case_root, "sid-z-live",
                                                   front_record["delivery_id"]))
                    front_observation = store.get(front_duty["id"])["observation"] or {}
                    self.assertEqual(front_observation.get("transfer_ref"),
                                     transfer_ref if transfer_state else None)
                    if case in {"queued", "unverified", "pending"}:
                        self.assertEqual(transfer_reads, [transfer_ref, transfer_ref])
                    if case == "pending":
                        self.assertEqual(schedule.call_count, 2)
                    else:
                        schedule.assert_not_called()

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
        attempt_id = "att-stopped-owner-" + harness
        metadata = {"attempt_schema_version": "2", "attempt_id": attempt_id,
            "dispatch_depth": "1", "worker_type": "owner", "parent_sid": "idle-parent",
            "harness": harness, "parent_harness": harness, "parent_completion_delivery": carrier,
            "note": "dead-worker-blocked", "delivery_intent": "1",
            "delivery_id": "delivery-original-" + harness}
        self.jobs.write_text("2026-10-18T01:00:00Z\tdone\t/r\t/w\towner\t" +
                             ",".join(f"{k}={v}" for k,v in metadata.items()) + "\n")
        identity = {"jobs": str(self.jobs), "attempt_id": attempt_id,
                    "session_id": "idle-parent", "harness": harness, "server": "default"}
        old = self.store.create(obligations.stable_duty_id("registered-batch", identity),
                                 "registered-batch", identity, {"carrier": carrier})
        self.store.update(old["id"], state="complete", delivery="acked", cleanup="complete")
        answers = [{"id": "input-retained", "digest": "kept", "text": "continue approved scope"}]
        with mock.patch("dispatch_owner_input.retained", return_value=answers), \
             mock.patch("dispatch_owner_input.blocked_owner_answers", return_value=answers), \
             mock.patch("dispatch_replacement._replacement_in_flight", return_value=False), \
             mock.patch.object(obligations, "ensure_normal_runner") as ensure:
            result = supervision.handover_identity_refusal(self.jobs, attempt_id,
                {"reason": "replacement-parent-identity-unproven"})
            self.assertTrue(result["parent_notified"])
            records = list(pending.record_directory(self.root, "idle-parent").glob("*.json"))
            parsed_records = [json.loads(path.read_text()) for path in records]
            matching = [value for value in parsed_records if value.get("recipient_kind") == carrier]
            self.assertTrue(matching, f"harness={harness} records={[(r.get('recipient_kind'), r.get('delivery_id')) for r in parsed_records]}")
            record = matching[0]
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
                    self.assertEqual((latest.get("observation") or {}).get("transfer_ref"), "a" * 32,
                        f"carrier={duty['intent']['carrier']} sends={len(sent)}")
                    peer_steward._reconcile_obligations(self.store, [latest])
                    self.assertEqual(len(sent), 1)
                    self.assertEqual(sent[0][0], (harness, "idle-parent"))
                    self.assertIn("scheduled the route's start under the parent's identity", sent[0][1])
                    self.assertEqual(sent[0][2]["schema"], "registered-runtime-completion-v1")
                    self.assertEqual(sent[0][2]["attempt_id"], duty["intent"]["identity"]["attempt_id"])
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

    @unittest.skipUnless(_PIDFD_HANDOFF, "pidfd handoff unavailable")
    def test_unsupported_observer_releases_same_lock_without_losing_accepted_duties(self):
        self._check_observer_handoff(supported=False)

    @unittest.skipUnless(_PIDFD_HANDOFF, "pidfd handoff unavailable")
    def test_supported_observer_keeps_its_pid_and_same_lock(self):
        self._check_observer_handoff(supported=True)

    @unittest.skipUnless(_PIDFD_HANDOFF, "pidfd handoff unavailable")
    def test_claude_only_observer_is_replaced_for_shared_activation_delivery(self):
        self._check_observer_handoff(supported=False, native_only=True)

    @unittest.skipUnless(_PIDFD_HANDOFF, "pidfd handoff unavailable")
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


class CanonicalNoticeJoinTest(unittest.TestCase):
    """Real zero-attempt writer, current duty, common courier and native boundaries."""
    setUp = ObligationStoreTest.setUp
    tearDown = ObligationStoreTest.tearDown
    _native_first_write = ObligationStoreTest._native_first_write

    def notices(self, harness="claude", sid="canonical-A", count=1, *, label="trial", offset=0,
                exit_code=0, lineage=None):
        import session_notice
        records = []
        with mock.patch.object(obligations, "ensure_normal_runner", return_value=False), \
             mock.patch.object(obligations, "request_dispatch_reconciliation", return_value=False):
            for index in range(offset, offset + count):
                run = f"host-20261025-12{index:02d}00-{label}"
                records.append(session_notice.notify(harness, sid, key="compute-run:" + run,
                    subject="compute run", jobs=self.jobs, lineage=lineage,
                    text=f"run {run} on host ended with exit {exit_code}; read it with: compute-hosts tail {run}"))
        self.assertTrue(all(record["attempt_ids"] == [] and record["parent_attempt_id"] == ""
                            and record["row_revisions"] == {} for record in records))
        return records

    def duties(self, sid=None):
        return [duty for duty in self.store.list(kinds={"run-notice"}, states=obligations._PENDING_STATES | {"complete", "cancelled"})
                if sid is None or duty["intent"]["identity"]["storage_recipient"] == sid]

    def edge(self, harness, origin, target, *, source="tidy-clear"):
        import session_tidy
        session_tidy._append_ledger_entry(session_tidy.Seat("pane", "canonical-" + origin),
            {"event": "succession", "source": source, "harness": harness,
             "from": {"harness": harness, "sid": origin}, "sid": target, "ts": 1})

    def parent_row(self, sid, *, harness="claude", index=0):
        from route_identity import route_hash
        route = {"route_id": "rt-canonical-" + str(index), "nodes": [{"id": "execute"}]}
        route["route_hash"] = route_hash(route)
        path = self.root / (route["route_id"] + ".json")
        path.write_text(json.dumps(route))
        from harness_capabilities import parent_completion
        meta = {"attempt_schema_version": "2", "attempt_id": "att-canonical-" + str(index),
            "parent_sid": sid, "parent_harness": harness, "harness": harness,
            "parent_completion_delivery": parent_completion(harness)["carrier"],
            "dispatch_depth": "1", "worker_type": "owner", "registered_worker": "1",
            "route_file": str(path), "route_id": route["route_id"],
            "route_hash": route["route_hash"], "route_node": "execute"}
        with self.jobs.open("a") as handle:
            handle.write("now\topen\t/fixture\t/fixture\towner\t" +
                         ",".join(key + "=" + value for key, value in meta.items()) + "\n")
        return meta

    def courier(self, harness, sid, *, receive=False):
        import contextlib
        from types import SimpleNamespace
        @contextlib.contextmanager
        def native():
            peer = peer_steward.peer_message
            sent = []
            def prepare(args, *, expected_recipient, on_prepared, producer_binding):
                body = Path(args.body_file).read_text()
                wire, ref = peer.prepare_peer_message(body,
                    {"harness": "runtime", "session_id": None, "name": "Hearting runtime"},
                    {"harness": expected_recipient[0], "session_id": expected_recipient[1]},
                    defer=True, refs=args.ref, producer_binding=producer_binding)
                on_prepared(ref)
                sent.append(peer._read_pending(ref))
                if receive:
                    self.assertEqual(peer.receive_peer_message(wire, {
                        "harness": expected_recipient[0], "session_id": expected_recipient[1]}), 0)
                return 0 if receive else 3
            census = {"result": {"agents": [{"agent": harness, "pane_id": "w1:pCanonical",
                      "agent_session": {"value": sid}}]}}
            with mock.patch.object(peer_steward, "_herdr_argv", return_value=["private-herdr"]), \
                 mock.patch.object(peer_steward.subprocess, "run", return_value=SimpleNamespace(
                    returncode=0, stdout=json.dumps(census))), \
                 mock.patch.object(peer_steward, "_resolve_target", return_value=(harness, sid)), \
                 mock.patch.object(peer_steward, "cmd_prompt", side_effect=prepare):
                yield sent
        return native()

    def transfer(self, harness="claude", sid="canonical-A", count=1, *, old_schema=False, referenced=False):
        import dispatch_pending_delivery as pending
        import dispatch_session_sweep as sweep
        records = self.notices(harness, sid, count)
        by_id = {duty["intent"]["delivery_id"]: duty for duty in self.duties(sid)}
        duties = [by_id[record["delivery_id"]] for record in records]
        claimed = [pending.claim(self.root, sid, record["delivery_id"],
            claim_owner="retained-rewake:" + duty["id"], lease_seconds=30, jobs=self.jobs)
            for record, duty in zip(records, duties)]
        body = sweep.delivery_context([(self.root, [{**record, sweep.STORAGE_KEY: sid,
            sweep.IDENTITY_KEY: pending.received_ack_identity(record, sid, record["delivery_id"])}
            for record in claimed])])
        recipient = {"harness": harness, "session_id": sid}
        if old_schema:
            self.assertEqual(count, 1)
            intent = duties[0]["intent"]
            producer = {"schema": "retained-runtime-notice-v1", "jobs": str(self.jobs),
                "duty_id": duties[0]["id"], "recipient": recipient,
                **{key: intent[key] for key in ("carrier", "delivery_id", "receipt_digest", "route_id", "route_node")}}
        else:
            producer = {"schema": "session-notice-runtime-v1", "jobs": str(self.jobs),
                "duty_id": duties[0]["id"], "recipient": recipient,
                "records": [{"duty_id": duty["id"], "storage_sid": sid,
                    "delivery_id": record["delivery_id"], "receipt_digest": record["receipt_digest"]}
                    for record, duty in zip(records, duties)]}
        refs = [value for record, duty in zip(records, duties) for value in (duty["id"], record["delivery_id"])]
        peer = peer_steward.peer_message
        with mock.patch.object(peer, "_reference_access_evidence_valid", return_value=referenced):
            wire, ref = peer.prepare_peer_message(body + ("\n" + "padding " * 400 if referenced else ""),
                {"harness": "runtime", "session_id": None, "name": "Hearting runtime"},
                recipient, defer=True, refs=refs, producer_binding=producer)
        for duty in duties:
            self.store.update(duty["id"], observation={"transfer_ref": ref,
                "transfer_bindings": {ref: producer}, "producer_binding": producer})
        return ref, recipient, claimed, duties

    def test_confirmed_successor_uses_one_original_duty_on_all_harnesses(self):
        from dispatch_seat_handover import record_recipient_binding
        for harness in ("claude", "codex", "opencode"):
            with self.subTest(harness=harness):
                old, current = "canonical-" + harness, "current-" + harness
                record, = self.notices(harness, old)
                duty, = self.duties(old)
                self.edge(harness, old, current)
                self.assertEqual(record_recipient_binding(record, old, harness=harness), (harness, current))
                self.assertEqual(obligations._ordinary_notice_target(duty["intent"]["identity"],
                    duty["intent"]["carrier"]), (harness, current, old))
                with self.courier(harness, current, receive=True) as sent:
                    peer_steward._reconcile_obligations(self.store, [duty])
                self.assertEqual(len(sent), 1)
                self.assertIn(record["delivery_id"], sent[0]["text"])
                self.assertEqual(self.store.get(duty["id"])["state"], "complete")
                self.assertEqual(len(self.duties(old)), 1)

    def test_provenance_retire_and_tidy_share_original_storage_and_one_courier(self):
        import dispatch_seat_handover as handover
        import dispatch_pending_delivery as pending
        import session_tidy as tidy
        old, holder, current = "provenance-codex", "retired-claude", "tidied-claude"
        record, = self.notices("codex", old)
        duty, = self.duties(old)
        frozen = pending.received_ack_identity(record, old, record["delivery_id"])
        seat = tidy.Seat("pane", "provenance-retire")
        tidy._append_ledger_entry(seat, {"event": "handover", "source": "retire",
            "from": {"harness": "codex", "sid": old}, "harness": "claude",
            "sid": holder, "bindings": [], "ts": 2})
        self.edge("claude", holder, current)
        self.assertEqual(handover.record_recipient_binding(record, old, harness="claude"),
                         ("claude", current))
        with mock.patch.object(handover, "pane_seat", return_value=seat):
            sources = dict(handover.storage_recipients(current, harness="claude"))
        self.assertIn(old, sources)
        self.assertTrue(getattr(sources[old], "lineage_notice", False))
        self.assertEqual(sources[old], frozenset(), "retire does not grant unbound jobs")
        with self.courier("claude", current, receive=True) as sent:
            peer_steward._reconcile_obligations(self.store, [duty])
        self.assertEqual(len(sent), 1)
        settled = pending.read(self.root, old, record["delivery_id"])
        self.assertEqual(settled["state"], "acked")
        self.assertEqual(pending.received_ack_identity(settled, old, record["delivery_id"]), frozen)
        self.assertEqual(self.store.get(duty["id"])["state"], "complete")
        self.assertEqual(len(self.duties(old)), 1)

    def test_provenance_retire_fork_never_restores_original_or_changes_exact_job_holder(self):
        import dispatch_seat_handover as handover
        import session_tidy as tidy
        old, holder = "provenance-fork-A", "provenance-fork-B"
        record, = self.notices("codex", old)
        meta = self.parent_row(old, harness="codex", index=90)
        binding = {"jobs": str(self.jobs), "route": meta["route_id"],
            "hash": meta["route_hash"], "node": "execute", "attempt": meta["attempt_id"], "parent": old}
        tidy._append_ledger_entry(tidy.Seat("pane", "exact-job"),
            {"event": "handover", "source": "retire", "harness": "codex",
             "sid": "exact-job-Z", "from": old, "bindings": [binding], "ts": 1})
        tidy._append_ledger_entry(tidy.Seat("pane", "provenance-fork"),
            {"event": "handover", "source": "retire", "harness": "claude",
             "sid": holder, "from": {"harness": "codex", "sid": old}, "bindings": [], "ts": 2})
        self.edge("claude", holder, "provenance-fork-C")
        self.edge("claude", holder, "provenance-fork-D")
        self.assertIsNone(handover.record_recipient_binding(record, old, harness="codex"))
        self.assertEqual(handover.effective_parent(meta, self.jobs), "exact-job-Z")
        self.assertEqual(handover.effective_parent_harness(meta, self.jobs), "codex")
        duty, = self.duties(old)
        with self.courier("codex", old, receive=True) as sent:
            peer_steward._reconcile_obligations(self.store, [duty])
        self.assertEqual(sent, [])

    def test_discovery_of_provenance_notice_from_legacy_retire_without_registry_or_duty(self):
        import session_tidy as tidy
        import dispatch_pending_delivery as pending
        old, current = "legacy-retire-discovery-codex", "legacy-retire-discovery-claude"
        record, = self.notices("codex", old)
        duty, = self.duties(old)
        original = pending.read(self.root, old, record["delivery_id"])
        (self.store.root / (duty["id"] + ".json")).unlink()
        self.jobs.unlink()  # Only this fixture's empty registry.
        tidy._append_ledger_entry(tidy.Seat("pane", "legacy-retire-discovery"), {
            "event": "handover", "source": "retire", "from": old,
            "harness": "claude", "sid": current, "bindings": [], "ts": 1})
        self.assertTrue(peer_steward._discover_session_notice_duties(self.store))
        self.assertEqual([item["id"] for item in self.duties(old)], [duty["id"]])
        self.assertEqual(pending.read(self.root, old, record["delivery_id"]), original)
        with self.courier("claude", current, receive=True) as sent:
            peer_steward._reconcile_obligations(self.store, self.duties(old))
        self.assertEqual(len(sent), 1)
        self.assertEqual(pending.read(self.root, old, record["delivery_id"])["state"], "acked")

    def _intermediate_retire_notice(self, harness, *, first_source):
        import dispatch_seat_handover as handover
        import dispatch_pending_delivery as pending
        import dispatch_session_sweep as sweep
        import session_tidy as tidy
        prefix = "intermediate-" + first_source + "-" + harness
        old, intermediate, current = (prefix + suffix for suffix in ("-A", "-B", "-C"))
        record, = self.notices(harness, intermediate)
        duty, = self.duties(intermediate)
        path = pending.record_path(self.root, intermediate, record["delivery_id"])
        original = path.read_bytes()
        identity = pending.received_ack_identity(record, intermediate, record["delivery_id"])
        (self.store.root / (duty["id"] + ".json")).unlink()  # Require real discovery.
        meta = self.parent_row(old, harness=harness, index=100 + len(self.duties()))
        binding = handover.binding_of(self.jobs, meta)
        self.assertEqual(binding["parent"], old)
        seat = tidy.Seat("pane", prefix)
        if first_source == "tidy-clear":
            self.edge(harness, old, intermediate)
        else:
            tidy._append_ledger_entry(seat, dict(event="handover", source="retire",
                harness=harness, sid=intermediate, bindings=[binding], ts=1, **{"from": old}))
        # The inherited job's parent stays A even though this retire originates at B.
        tidy._append_ledger_entry(seat, dict(event="handover", source="retire",
            harness=harness, sid=current, bindings=[binding], ts=2, **{"from": intermediate}))
        for origin in (old, intermediate):
            self.assertEqual(handover.session_successor(origin), (current, harness))
            self.assertEqual(handover.session_successor(origin, harness=harness), (current, harness))
        self.assertEqual(handover.effective_parent(meta, self.jobs), current)
        self.assertEqual(handover.record_recipient_binding(record, intermediate, harness=harness),
                         (harness, current))
        with mock.patch.object(handover, "pane_seat", return_value=seat):
            scopes = dict(handover.storage_recipients(current, harness=harness))
            self.assertEqual(scopes[old], {meta["attempt_id"]})
            self.assertEqual(scopes[intermediate], frozenset(), "B grants notices, not A's jobs")
            self.assertTrue(scopes[intermediate].lineage_notice)
            addressed, _count = sweep.addressed_records(self.root, record["recipient_kind"], current)
        self.assertEqual([(item[sweep.STORAGE_KEY], item["delivery_id"]) for item in addressed],
                         [(intermediate, record["delivery_id"])])
        self.assertTrue(peer_steward._discover_session_notice_duties(self.store))
        self.assertEqual([item["id"] for item in self.duties(intermediate)], [duty["id"]])
        # Discovery preserves all original bytes, including claim/lease/fence/ACK state.
        self.assertEqual(path.read_bytes(), original)
        self.assertIsNone(pending.read_submission_fence(self.root, intermediate, record["delivery_id"],
            expected_identity=pending.submission_fence_identity(record, current)))
        with mock.patch.object(pending, "claim", wraps=pending.claim) as claims, \
             mock.patch.object(pending, "ack_received", wraps=pending.ack_received) as acks, \
             self.courier(harness, current, receive=True) as sent:
            peer_steward._reconcile_obligations(self.store, self.duties(intermediate))
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["producer_binding"]["records"], [{"duty_id": duty["id"],
            "storage_sid": intermediate, "delivery_id": record["delivery_id"],
            "receipt_digest": record["receipt_digest"]}])
        self.assertTrue(claims.call_args_list)
        self.assertTrue(acks.call_args_list)
        for call in claims.call_args_list + acks.call_args_list:
            self.assertEqual(call.args[:3], (self.root, intermediate, record["delivery_id"]))
        settled = pending.read(self.root, intermediate, record["delivery_id"])
        self.assertEqual(settled["state"], "acked")
        self.assertEqual(pending.received_ack_identity(settled, intermediate, record["delivery_id"]), identity)
        self.assertEqual(self.store.get(duty["id"])["state"], "complete")
        for sid in (old, current):
            self.assertIsNone(pending.read(self.root, sid, record["delivery_id"]))

    def test_intermediate_notice_after_two_legacy_retires_keeps_original_storage(self):
        for harness in ("claude", "codex", "opencode"):
            with self.subTest(harness=harness):
                self._intermediate_retire_notice(harness, first_source="retire")

    def test_intermediate_notice_after_tidy_then_legacy_retire_keeps_original_storage(self):
        for harness in ("claude", "codex", "opencode"):
            with self.subTest(harness=harness):
                self._intermediate_retire_notice(harness, first_source="tidy-clear")

    def test_sid_only_successor_refuses_same_sid_in_two_qualified_incoming_targets(self):
        import dispatch_seat_handover as handover
        sid = "ambiguous-incoming-target"
        self.edge("claude", "claude-origin", sid)
        self.edge("codex", "codex-origin", sid)
        with self.assertRaisesRegex(OSError, "notice-lineage-unresolved"):
            handover.session_successor(sid)
        for harness in ("claude", "codex"):
            self.assertEqual(handover.session_successor(sid, harness=harness), ("", ""))

    def test_legacy_retire_successor_refuses_fork_merge_cycle_and_unreadable(self):
        import dispatch_seat_handover as handover
        import session_tidy as tidy
        for kind in ("fork", "merge", "cycle", "unreadable"):
            with self.subTest(kind=kind):
                old, middle, current = (kind + "-legacy-" + suffix for suffix in ("A", "B", "C"))
                seat = tidy.Seat("pane", kind + "-legacy-control")
                pairs = [(old, middle), (middle, current)]
                if kind != "unreadable":
                    pairs.append({"fork": (middle, current + "-other"),
                                  "merge": (old + "-other", current), "cycle": (current, old)}[kind])
                for ts, (origin, target) in enumerate(pairs):
                    tidy._append_ledger_entry(seat, dict(event="handover", source="retire",
                        harness="codex", sid=target, bindings=[], ts=ts, **{"from": origin}))
                if kind == "unreadable":
                    tidy._ledger_path(seat).write_bytes(b"{unreadable")
                for origin in (old, middle, current):
                    for harness in (None, "codex"):
                        with self.assertRaises(OSError):
                            handover.session_successor(origin, harness=harness)

    def test_provenance_same_sid_in_another_harness_does_not_transfer_authority(self):
        import dispatch_seat_handover as handover
        sid = "same-sid-two-harnesses"
        record, = self.notices("codex", sid)
        self.edge("claude", sid, "claude-only-successor")
        self.assertEqual(handover.record_recipient_binding(record, sid, harness="codex"),
                         ("codex", sid))
        self.edge("codex", sid, "codex-only-successor")
        self.assertEqual(handover.record_recipient_binding(record, sid, harness="codex"),
                         ("codex", "codex-only-successor"))

    def test_current_watch_producer_keeps_ordinary_family_grouping(self):
        from types import SimpleNamespace
        spec = importlib.util.spec_from_file_location("canonical_watch_producer", _HERE / "compute-hosts.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        with mock.patch.object(obligations, "ensure_normal_runner", return_value=False), \
             mock.patch.object(obligations, "request_dispatch_reconciliation", return_value=False):
            for index in range(4):
                run = f"host-20261025-12{index:02d}00-trial"
                directory = self.root / "private-ended-runs" / run
                directory.mkdir(parents=True)
                (directory / "meta.json").write_text(json.dumps({"run_id": run, "host": "host",
                    "provenance": {"session": {"harness": "claude", "id": "canonical-A"}}}))
                (directory / "exit_code").write_text("0")
                self.assertEqual(module.cmd_watch_run(SimpleNamespace(run_dir=str(directory)), now=lambda: 0,
                    sleep=lambda _seconds: self.fail("fixture is already ended")), 0)
        duties = self.duties()
        self.assertEqual(len(duties), 4)
        with self.courier("claude", "canonical-A", receive=True) as sent:
            peer_steward._reconcile_obligations(self.store, duties)
        self.assertEqual(len(sent), 1)
        self.assertIn("compute run summary: 4 runs", sent[0]["text"])
        self.assertEqual(sent[0]["text"].count("HEARTING_RECEIPT_V1 "), 4)
        self.assertTrue(all(duty["intent"]["delivery_id"] in sent[0]["text"] for duty in duties))

    def test_checkpoint_and_experiment_exit_keep_their_own_lines_and_receipts(self):
        import session_notice
        records = self.notices(count=3)
        with mock.patch.object(obligations, "ensure_normal_runner", return_value=False):
            for event in ("checkpoint", "exit"):
                records.append(session_notice.notify("claude", "canonical-A", jobs=self.jobs,
                    key="experiment:own-run:" + event + ":1:claude:canonical-A",
                    subject="experiment " + event + " (1/1)", text="own-run " + event + ": keep metrics and timing"))
        with self.courier("claude", "canonical-A", receive=True) as sent:
            peer_steward._reconcile_obligations(self.store, self.duties())
        self.assertEqual(len(sent), 1)
        body = sent[0]["text"]
        self.assertEqual(body.count("compute run summary: 3 runs"), 1)
        for event in ("checkpoint", "exit"):
            self.assertIn("experiment " + event + " (1/1): own-run " + event + ": keep metrics and timing", body)
        self.assertEqual(body.count("HEARTING_RECEIPT_V1 "), 5)
        self.assertTrue(all(record["delivery_id"] in body for record in records))

    def test_grouping_keeps_experiments_failures_unknown_and_every_marker(self):
        import session_notice
        records = self.notices(count=3) + self.notices(count=3, label="other")
        records += self.notices(offset=10, exit_code=1)
        with mock.patch.object(obligations, "ensure_normal_runner", return_value=False):
            records.append(session_notice.notify("claude", "canonical-A", key="unknown", subject="compute run",
                text="unclassified exit observation", jobs=self.jobs))
        with self.courier("claude", "canonical-A", receive=True) as sent:
            peer_steward._reconcile_obligations(self.store, self.duties())
        self.assertEqual(len(sent), 1)
        body = sent[0]["text"]
        self.assertEqual(body.count("compute run summary: 3 runs"), 2)
        self.assertIn("label=trial", body)
        self.assertIn("label=other", body)
        self.assertIn("ended with exit 1", body)
        self.assertIn("unclassified exit observation", body)
        self.assertEqual(body.count("HEARTING_RECEIPT_V1 "), 8)
        self.assertTrue(all(record["delivery_id"] in body for record in records))

    def test_first_eight_then_four_and_other_recipient_progress(self):
        self.notices(sid="A", count=12)
        self.notices(sid="Z")
        selection, counts = {}, []
        for sid in ("A", "Z", "A"):
            with self.courier("claude", sid, receive=True) as sent:
                peer_steward._reconcile_obligations(self.store, self.duties(),
                    notice_selection=selection, runner_incarnation="canonical-test")
            self.assertEqual(len(sent), 1)
            counts.append(sent[0]["text"].count("HEARTING_RECEIPT_V1 "))
        self.assertEqual(counts, [8, 1, 4])
        self.assertTrue(all(duty["state"] == "complete" for duty in self.duties()))

    def test_each_canonical_member_rechecks_owner_identity_lease_and_fence(self):
        import dispatch_pending_delivery as pending
        for harness in ("claude", "codex"):
            for count in (1, 4, 8):
                for index in range(count):
                    for mutation in ("owner", "identity", "lease", "fence", "float-lease", "boolean-lease"):
                        with self.subTest(harness=harness, count=count, index=index, mutation=mutation):
                            sid = f"canonical-{harness}-{count}-{index}-{mutation}"
                            ref, recipient, records, duties = self.transfer(harness, sid, count)
                            original = records[index]
                            did = original["delivery_id"]
                            def change():
                                if mutation == "owner":
                                    pending.release_claim(self.root, sid, did, claim_owner=original["claim_owner"])
                                    pending.claim(self.root, sid, did, claim_owner="foreign-owner", lease_seconds=30,
                                        jobs=self.jobs, retry_context="foreign")
                                elif mutation == "fence":
                                    pending.prepare_submission_fence(self.root, sid, did,
                                        claim_owner=original["claim_owner"],
                                        expected_identity=pending.submission_fence_identity(original, sid))
                                else:
                                    path = pending.record_path(self.root, sid, did)
                                    raw = json.loads(path.read_text())
                                    raw.update({"session_generation": "replacement"} if mutation == "identity" else
                                        {"claim_deadline_ns": float(time.monotonic_ns() + 30_000_000_000)} if mutation == "float-lease" else
                                        {"claim_deadline_ns": True} if mutation == "boolean-lease" else
                                        {"claim_deadline_ns": time.monotonic_ns() - 1})
                                    path.write_text(json.dumps(raw))
                            with mock.patch.object(pending, "renew_unsubmitted_claim", return_value=original) if mutation == "lease" else contextlib.nullcontext():
                                self.assertEqual(self._native_first_write(ref, recipient, before=change), [])

    def test_queued_notice_context_never_grants_another_first_write(self):
        peer = peer_steward.peer_message
        for old_schema in (False, True):
            for state, reason in (("queued", "hook-context-emitted"), ("queued", "native-queue-accepted"),
                                  ("unverified", "native-inflight")):
                with self.subTest(old_schema=old_schema, state=state, reason=reason):
                    ref, recipient, records, duties = self.transfer(sid="queued-" + str(old_schema) + reason,
                        old_schema=old_schema)
                    row = peer._read_pending(ref)
                    peer._save_pending(dict(row, state=state, receipt=reason))
                    reservation = peer.reserve_pending_herdr(ref, recipient, receipt="hook-context-inflight",
                        allow_hook_context_recovery=True)
                    if reservation is not None:
                        self.assertIsNone(peer.begin_pending_write(reservation, recipient=recipient,
                            receipt="hook-context-inflight"))
                    self.assertFalse(obligations.registered_submission_current(peer._read_pending(ref)))

    def test_retained_single_notice_schema_uses_the_same_first_write_guard(self):
        import dispatch_pending_delivery as pending
        for harness in ("claude", "codex"):
            for mutation in ("none", "owner", "identity", "fence"):
                with self.subTest(harness=harness, mutation=mutation):
                    sid = "single-" + harness + mutation
                    ref, recipient, records, duties = self.transfer(harness, sid, old_schema=True)
                    original, = records
                    def change():
                        path = pending.record_path(self.root, sid, original["delivery_id"])
                        if mutation == "owner":
                            pending.release_claim(self.root, sid, original["delivery_id"], claim_owner=original["claim_owner"])
                            pending.claim(self.root, sid, original["delivery_id"], claim_owner="other-owner", lease_seconds=30,
                                          jobs=self.jobs, retry_context="other")
                        elif mutation == "identity":
                            raw = json.loads(path.read_text()); raw["session_generation"] = "replacement"
                            path.write_text(json.dumps(raw))
                        elif mutation == "fence":
                            pending.prepare_submission_fence(self.root, sid, original["delivery_id"],
                                claim_owner=original["claim_owner"], expected_identity=pending.submission_fence_identity(original, sid))
                    self.assertEqual(len(self._native_first_write(ref, recipient, before=change)), int(mutation == "none"))

    def test_original_receipt_survives_handover_unknown_graph_and_ack_retry(self):
        import dispatch_pending_delivery as pending
        import session_tidy
        peer = peer_steward.peer_message
        for old_schema in (False, True):
            for referenced in (False, True):
                with self.subTest(old_schema=old_schema, referenced=referenced):
                    sid = "receipt-" + str(old_schema) + str(referenced)
                    ref, recipient, records, duties = self.transfer(sid=sid,
                        count=1 if old_schema else 4, old_schema=old_schema, referenced=referenced)
                    wire = peer._read_pending(ref)["text"]
                    seal = peer._transfer_path(ref).read_bytes()
                    self.edge("claude", sid, sid + "-next")
                    bad = session_tidy.state_root() / "sessions" / (sid + "-unknown.jsonl")
                    bad.parent.mkdir(parents=True, exist_ok=True); bad.write_text("{")
                    for record in records:
                        path = pending.record_path(self.root, sid, record["delivery_id"])
                        raw = json.loads(path.read_text()); raw.update(claim_owner="new-owner", claim_deadline_ns=1)
                        path.write_text(json.dumps(raw))
                    with mock.patch.object(pending, "ack_received", side_effect=OSError("ACK unavailable")):
                        self.assertEqual(peer.receive_peer_message(wire, recipient), 0)
                    received = peer._read_pending(ref)
                    self.assertEqual(received["text"], wire)
                    self.assertTrue(all(pending.read(self.root, sid, record["delivery_id"])["state"] == "claimed"
                                        for record in records))
                    with mock.patch.object(peer, "prepare_peer_message", side_effect=AssertionError("replay")), \
                         mock.patch.object(obligations, "_ordinary_notice_target", side_effect=AssertionError("historical ACK needs no current graph")):
                        self.assertTrue(peer._ack_registered_delivery(received))
                    self.assertTrue(all(pending.read(self.root, sid, record["delivery_id"])["state"] == "acked"
                                        for record in records))
                    self.assertEqual(peer._transfer_path(ref).read_bytes(), seal)
                    bad.unlink()  # This test's private malformed ledger only.

    def test_retained_single_notice_receipt_preserves_concurrent_terminal_duty(self):
        import dispatch_pending_delivery as pending
        peer = peer_steward.peer_message
        for terminal in ("complete", "cancelled"):
            with self.subTest(terminal=terminal):
                sid = "single-terminal-" + terminal
                ref, recipient, records, duties = self.transfer(sid=sid, old_schema=True)
                duty, = duties
                cls = obligations.ObligationStore
                original_lock = cls._lock
                snapshots = []
                active = False
                def lock(store, duty_id):
                    nonlocal active
                    if duty_id == duty["id"] and not snapshots and not active:
                        active = True
                        try:
                            other = cls(self.root)
                            terminal_duty = other.update(duty_id, state=terminal,
                                delivery="already-settled", cleanup="complete",
                                observation={"phase": "settled", "transfer_ref": "f" * 32,
                                             "checkpoint": "concurrent"})
                            path = other._record_path(duty_id)
                            snapshots.append((terminal_duty, path.read_bytes(), path.stat().st_mtime_ns))
                        finally:
                            active = False
                    return original_lock(store, duty_id)
                with mock.patch.object(cls, "_lock", lock):
                    self.assertEqual(peer.receive_peer_message(peer._read_pending(ref)["text"], recipient), 0)
                self.assertEqual(len(snapshots), 1)
                self.assertEqual(self.store.get(duty["id"]), snapshots[0][0])
                path = self.store._record_path(duty["id"])
                self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), snapshots[0][1:])
                self.assertEqual(pending.read(self.root, sid, records[0]["delivery_id"])["state"], "acked")

    def test_same_digest_replacement_is_not_adopted_at_group_or_single_receipt(self):
        import dispatch_pending_delivery as pending
        peer = peer_steward.peer_message
        for old_schema in (False, True):
            for referenced in (False, True):
                with self.subTest(old_schema=old_schema, referenced=referenced):
                    sid = "replacement-" + str(old_schema) + str(referenced)
                    ref, recipient, records, duties = self.transfer(sid=sid,
                        count=1 if old_schema else 4, old_schema=old_schema, referenced=referenced)
                    original = records[0]
                    path = pending.record_path(self.root, sid, original["delivery_id"])
                    raw = json.loads(path.read_text()); raw["session_generation"] = "replacement"
                    path.write_text(json.dumps(raw))
                    self.assertEqual(peer.receive_peer_message(peer._read_pending(ref)["text"], recipient), 0)
                    replacement = pending.read(self.root, sid, original["delivery_id"])
                    self.assertEqual(replacement["receipt_digest"], original["receipt_digest"])
                    self.assertNotEqual(replacement["state"], "acked")
                    self.assertTrue(all(pending.read(self.root, sid, record["delivery_id"])["state"] == "acked"
                                        for record in records[1:]))

    def test_exact_pending_renewal_keeps_original_owner_identity_and_count(self):
        import dispatch_pending_delivery as pending
        for old_schema in (False, True):
            for harness in ("claude", "codex"):
                sid = "renew-" + harness + str(old_schema)
                ref, recipient, records, duties = self.transfer(harness, sid, old_schema=old_schema)
                record, = records
                path = pending.record_path(self.root, sid, record["delivery_id"])
                raw = json.loads(path.read_text()); raw["claim_deadline_ns"] = time.monotonic_ns() - 1
                path.write_text(json.dumps(raw))
                with mock.patch.object(pending, "renew_unsubmitted_claim", wraps=pending.renew_unsubmitted_claim) as renewal:
                    self.assertEqual(len(self._native_first_write(ref, recipient)), 1)
                self.assertEqual(renewal.call_count, 1)
                current = pending.read(self.root, sid, record["delivery_id"])
                self.assertGreater(current["claim_deadline_ns"], time.monotonic_ns())
                self.assertEqual(current["claim_owner"], record["claim_owner"])
                self.assertEqual(current["attempts"], record["attempts"])
                self.assertEqual(pending.received_ack_identity(current, sid, record["delivery_id"]),
                                 pending.received_ack_identity(record, sid, record["delivery_id"]))

    def test_explicit_binding_pair_precedes_tidy_and_depth_one_has_no_twelve_limit(self):
        import dispatch_seat_handover as handover
        import session_tidy
        self.edge("claude", "A", "tidy-B")
        for index in range(14):
            meta = self.parent_row("A", index=index)
            record, = self.notices(sid="A", label="authority-" + str(index), lineage={
                "kind": "attempt-parent", "harness": "claude", "session_id": "A", "attempt_id": meta["attempt_id"]})
            if index == 13:
                session_tidy._append_ledger_entry(session_tidy.Seat("pane", "canonical-explicit"),
                    {"event": "handover", "source": "retire", "harness": "codex", "sid": "A",
                     "from": {"harness": "claude", "sid": "A"}, "ts": 1,
                     "bindings": [handover.binding_of(self.jobs, meta)]})
                self.edge("codex", "A", "codex-C")
            expected = ("codex", "codex-C") if index == 13 else ("claude", "tidy-B")
            duty = next(d for d in self.duties("A") if d["intent"]["delivery_id"] == record["delivery_id"])
            self.assertEqual(handover.record_recipient_binding(record, "A", harness="claude", jobs=self.jobs), expected)
            self.assertEqual(obligations._ordinary_notice_target(duty["intent"]["identity"],
                duty["intent"]["carrier"]), (*expected, "A"))
        with self.courier("codex", "codex-C", receive=True) as sent:
            peer_steward._reconcile_obligations(self.store, [duty])
        self.assertEqual(len(sent), 1)

    def test_unknown_unchanged_duty_rechecks_new_lineage_before_backoff(self):
        record, = self.notices()
        duty, = self.duties()
        duty = self.store.update(duty["id"], state="unknown", observation={"reason": "recipient-lineage-unavailable"})
        retry = {duty["id"]: (peer_steward._obligation_retry_fingerprint(duty), time.monotonic() + 60, 1)}
        self.edge("claude", "canonical-A", "new-current")
        with self.courier("claude", "new-current", receive=True) as sent:
            peer_steward._reconcile_obligations(self.store, [duty], retry_state=retry)
        self.assertEqual(len(sent), 1)
        self.assertEqual(self.store.get(duty["id"])["state"], "complete")
        self.assertNotIn(duty["id"], retry)
        self.assertIn(record["delivery_id"], sent[0]["text"])

    def test_later_native_observation_rechecks_the_whole_canonical_body(self):
        import dispatch_pending_delivery as pending
        records = self.notices(count=4)
        calls = []
        def target(*args, **kwargs):
            calls.append(True)
            if len(calls) == 6:
                self.edge("claude", "canonical-A", "late-successor")
            return "claude", "canonical-A"
        with self.courier("claude", "canonical-A") as sent, \
             mock.patch.object(peer_steward, "_resolve_target", side_effect=target):
            peer_steward._reconcile_obligations(self.store, self.duties())
        self.assertGreaterEqual(len(calls), 6)
        self.assertEqual(sent, [])
        self.assertTrue(all(pending.read(self.root, "canonical-A", record["delivery_id"])["state"] == "pending"
                            for record in records))

    def test_writer_retry_uses_original_stored_lineage_and_one_logical_duty(self):
        import session_notice
        first = self.parent_row("A", index=0)
        second = self.parent_row("A", index=1)
        original = {"kind": "attempt-parent", "harness": "claude", "session_id": "A", "attempt_id": first["attempt_id"]}
        record, = self.notices(sid="A", lineage=original)
        with mock.patch.object(obligations, "ensure_normal_runner", return_value=False):
            session_notice.notify("claude", "A", key=record["receipt"]["key"], subject=record["receipt"]["subject"],
                text=record["receipt"]["text"], jobs=self.jobs,
                lineage={**original, "attempt_id": second["attempt_id"]})
        duty, = self.duties("A")
        self.assertEqual(duty["intent"]["identity"]["lineage"], original)
        self.assertEqual(duty["intent"]["delivery_id"], record["delivery_id"])

    def test_unknown_early_member_keeps_every_other_exact_original_receipt(self):
        import dispatch_pending_delivery as pending
        peer = peer_steward.peer_message
        ref, recipient, records, duties = self.transfer(count=4)
        path = pending.record_path(self.root, recipient["session_id"], records[0]["delivery_id"])
        raw = json.loads(path.read_text()); raw["lineage"] = ["unknown-authority"]
        path.write_text(json.dumps(raw))
        self.assertEqual(peer.receive_peer_message(peer._read_pending(ref)["text"], recipient), 0)
        self.assertNotEqual(pending.read(self.root, recipient["session_id"], records[0]["delivery_id"])["state"], "acked")
        self.assertTrue(all(pending.read(self.root, recipient["session_id"], record["delivery_id"])["state"] == "acked"
                            for record in records[1:]))

    def test_owner_session_keeps_exact_native_consumer_after_tidy(self):
        import dispatch_completion_join as join
        from dispatch_contract import dispatch_state_root
        from dispatch_seat_handover import record_recipient_binding
        meta = self.parent_row("registered-parent")
        state = dispatch_state_root(self.jobs) / "supervisor-state" / (meta["attempt_id"] + ".json")
        join._write_supervisor_state_unlocked(state, meta["attempt_id"], set(),
            resource={"session_id": "owner-A", "delivered": [], "outbox": None})
        record, = self.notices(sid="owner-A", lineage={"kind": "owner-session", "harness": "claude",
            "session_id": "owner-A", "attempt_id": meta["attempt_id"]})
        self.edge("claude", "owner-A", "owner-B")
        self.assertEqual(record_recipient_binding(record, "owner-A", harness="claude"), ("claude", "owner-A"))
        state.write_text("{")
        self.assertIsNone(record_recipient_binding(record, "owner-A", harness="claude"))

    def test_three_questions_share_one_pass_without_transferring_the_native_consumer(self):
        import dispatch_completion_join as join
        from dispatch_contract import dispatch_state_root
        import dispatch_seat_handover as handover
        import session_tidy as tidy
        from peer_read_context import PeerReadContext
        meta = self.parent_row("three-A")
        state = dispatch_state_root(self.jobs) / "supervisor-state" / (meta["attempt_id"] + ".json")
        join._write_supervisor_state_unlocked(state, meta["attempt_id"], set(),
            resource={"session_id": "three-A", "delivered": [], "outbox": None})
        self.edge("claude", "three-A", "three-seat")
        # Schema-1 exact job authority deliberately does not create a seat edge.
        tidy._append_ledger_entry(tidy.Seat("pane", "three-job"), {
            "event": "handover", "harness": "codex", "sid": "three-job", "ts": 2,
            "from": {"harness": "claude", "sid": "three-A"},
            "bindings": [handover.binding_of(self.jobs, meta)]})
        records = []
        for index, kind in enumerate(("provenance", "attempt-parent", "owner-session")):
            lineage = {"kind": kind, "harness": "claude", "session_id": "three-A"}
            if kind != "provenance":
                lineage["attempt_id"] = meta["attempt_id"]
            record, = self.notices(sid="three-A", label=kind, offset=index, lineage=lineage)
            records.append(record)
        frozen = [dict(record) for record in records]
        context = PeerReadContext()
        self.assertEqual([handover.record_recipient_binding(record, "three-A", harness="claude",
            jobs=self.jobs, read_context=context) for record in records],
            [("claude", "three-seat"), ("codex", "three-job"), ("claude", "three-A")])
        job = handover.resolve_recipient("job-holder", metadata=meta, jobs=self.jobs, read_context=context)
        self.assertEqual((job.session_id, job.harness, job.registered_session),
                         ("three-job", "codex", "three-A"))
        self.assertTrue(handover.owns(meta, "three-job", self.jobs, read_context=context))
        self.assertFalse(handover.owns(meta, "three-seat", self.jobs, read_context=context))
        self.assertEqual(records, frozen)
        with mock.patch.object(handover, "_readable_lineage_signature", side_effect=OSError("fixture outage")):
            self.assertEqual([handover.record_recipient_binding(record, "three-A", harness="claude",
                jobs=self.jobs) for record in records], [None, None, ("claude", "three-A")])
        state.write_text("{")
        self.assertIsNone(handover.record_recipient_binding(records[2], "three-A", harness="claude", jobs=self.jobs))

    def test_missing_contradictory_manual_and_fork_authority_stays_unresolved(self):
        import dispatch_seat_handover as handover
        import session_notice
        record, = self.notices()
        self.assertIsNone(handover.record_recipient_binding({**record, "lineage": []}, "canonical-A", harness="claude"))
        wrong = session_notice._stored_lineage(self.jobs, {"kind": "provenance", "harness": "codex", "session_id": "canonical-A"})
        self.assertIsNone(handover.record_recipient_binding({**record, "lineage": [wrong]}, "canonical-A", harness="claude"))
        for binding in (None, [], {"kind": "unknown", "harness": "claude", "session_id": "canonical-A"}):
            stored = session_notice.LINEAGE_PREFIX + json.dumps({"jobs": str(self.jobs), "binding": binding})
            self.assertIsNone(handover.record_recipient_binding({**record, "lineage": [stored]}, "canonical-A", harness="claude"))
        self.edge("claude", "canonical-A", "manual-B", source="manual-clear")
        self.assertEqual(handover.record_recipient_binding(record, "canonical-A", harness="claude"), ("claude", "canonical-A"))
        self.edge("claude", "canonical-A", "B")
        self.edge("claude", "canonical-A", "C")
        self.assertIsNone(handover.record_recipient_binding(record, "canonical-A", harness="claude"))



    def test_canonical_route_free_runner_releases_only_original_unsubmitted_group_claims(self):
        import dispatch_pending_delivery as pending
        import dispatch_session_sweep as sweep
        import session_notice
        from types import SimpleNamespace
        for index in range(4):
            for mutation in ("identity", "owner", "fence", "lease", "terminal"):
                with self.subTest(index=index, mutation=mutation):
                    sid = f"runner-{index}-{mutation}"
                    with mock.patch.object(obligations, "ensure_normal_runner", return_value=False):
                        records = [session_notice.notify("claude", sid, key=f"runner:{sid}:{n}",
                                   subject="done", text="completed", jobs=self.jobs) for n in range(4)]
                    duties = [duty for duty in self.duties()
                              if duty["intent"]["identity"]["storage_recipient"] == sid]
                    changed = records[index]["delivery_id"]
                    captured = {}
                    real_render = sweep.delivery_context
                    def render(groups):
                        record = pending.read(self.root, sid, changed)
                        if mutation == "owner":
                            pending.release_claim(self.root, sid, changed, claim_owner=record["claim_owner"])
                            pending.claim(self.root, sid, changed, claim_owner="other-carrier", lease_seconds=30,
                                          jobs=self.jobs, retry_context="new-owner")
                        elif mutation == "terminal":
                            pending.ack_received(self.root, sid, changed,
                                expected_identity=pending.received_ack_identity(record, sid, changed),
                                acked_by="already-exact")
                        elif mutation == "fence":
                            pending.prepare_submission_fence(self.root, sid, changed,
                                claim_owner=record["claim_owner"],
                                expected_identity=pending.submission_fence_identity(record, sid))
                        else:
                            path = pending.record_path(self.root, sid, changed)
                            raw = json.loads(path.read_text())
                            raw.update({"route_node": "replacement-node"} if mutation == "identity" else
                                       {"claim_deadline_ns": time.monotonic_ns() - 1})
                            path.write_text(json.dumps(raw))
                        path = pending.record_path(self.root, sid, changed)
                        captured["bytes"] = path.read_bytes()
                        return real_render(groups)
                    agents = {"result": {"agents": [{"agent": "claude", "pane_id": "w1:pNotice",
                              "agent_session": {"value": sid}}]}}
                    with mock.patch.object(peer_steward.subprocess, "run", return_value=SimpleNamespace(
                            returncode=0, stdout=json.dumps(agents))), \
                         mock.patch.object(peer_steward, "_resolve_target", return_value=("claude", sid)), \
                         mock.patch.object(sweep, "delivery_context", side_effect=render), \
                         mock.patch.object(peer_steward, "cmd_prompt") as prompt:
                        peer_steward._resume_session_notice_obligations(duties, self.store)
                    prompt.assert_not_called()
                    self.assertEqual(pending.record_path(self.root, sid, changed).read_bytes(), captured["bytes"])
                    self.assertTrue(all(pending.read(self.root, sid, r["delivery_id"])["state"] == "pending"
                                        for r in records if r["delivery_id"] != changed))

    def test_canonical_prepared_notice_group_recovers_after_later_handover_without_mutating_old_transfer(self):
        import dispatch_pending_delivery as pending
        import session_tidy as tidy
        from types import SimpleNamespace
        peer = peer_steward.peer_message
        os.environ["AGENT_PEER_LEDGER_ROOT"] = str(self.root)
        ref, recipient, records, duties = self.transfer("claude", "canonical-retarget", 4)
        sid, successor = recipient["session_id"], "retargeted-current-holder"
        seal = peer._transfer_path(ref).read_bytes()
        for record, duty in zip(records, duties):
            pending.mark_sent_ambiguous(self.root, sid, record["delivery_id"], claim_owner=record["claim_owner"])
            self.store.update(duty["id"], observation={**self.store.get(duty["id"])["observation"], "transfer_ref": ref})
        tidy._append_ledger_entry(tidy.Seat("pane", "prepared-retarget"),
            dict(event="succession", source="tidy-clear", harness="claude", sid=successor,
                 **{"from": {"harness": "claude", "sid": sid}}, ts=1))
        for record in records:
            path = pending.record_path(self.root, sid, record["delivery_id"])
            raw = json.loads(path.read_text()); raw["claim_deadline_ns"] = time.monotonic_ns() - 1
            path.write_text(json.dumps(raw))
        sent = []
        def courier(args, *, expected_recipient, on_prepared, producer_binding):
            self.assertEqual(expected_recipient, ("claude", successor))
            wire, new_ref = peer.prepare_peer_message(Path(args.body_file).read_text(),
                {"harness": "runtime", "session_id": None, "name": "Hearting runtime"},
                {"harness": "claude", "session_id": successor}, defer=True,
                refs=args.ref, producer_binding=producer_binding)
            on_prepared(new_ref); sent.append(new_ref)
            self.assertEqual(peer.receive_peer_message(wire, {"harness": "claude", "session_id": successor}), 0)
            return 0
        agents = {"result": {"agents": [{"agent": "claude", "pane_id": "w1:pRetarget",
                  "agent_session": {"value": successor}}]}}
        with mock.patch.object(peer_steward.subprocess, "run", return_value=SimpleNamespace(
                returncode=0, stdout=json.dumps(agents))), \
             mock.patch.object(peer_steward, "_resolve_target", return_value=("claude", successor)), \
             mock.patch.object(peer_steward, "cmd_prompt", side_effect=courier):
            peer_steward._resume_session_notice_obligations(self.duties(), self.store)
        self.assertEqual(len(sent), 1)
        self.assertNotEqual(sent[0], ref)
        self.assertEqual(peer._transfer_path(ref).read_bytes(), seal)
        self.assertEqual(peer._read_pending(ref)["state"], "pending")
        self.assertTrue(all(pending.read(self.root, sid, r["delivery_id"])["state"] == "acked" for r in records))

    def test_canonical_prepared_retarget_holds_group_for_changed_owner_fence_identity_or_live_lease(self):
        import dispatch_pending_delivery as pending
        import session_tidy as tidy
        from types import SimpleNamespace
        peer = peer_steward.peer_message
        os.environ["AGENT_PEER_LEDGER_ROOT"] = str(self.root)
        for mutation in ("owner", "fence", "identity", "queued", "unverified", "live-lease"):
            with self.subTest(mutation=mutation):
                ref, recipient, records, duties = self.transfer("claude", "canonical-held-" + mutation, 4)
                sid, successor = recipient["session_id"], "current-" + mutation
                last = max(records, key=lambda value: value["delivery_id"])
                for record, duty in zip(records, duties):
                    if mutation == "fence" and record["delivery_id"] == last["delivery_id"]:
                        # Fence the add while live, then retain it after expiry.
                        pending.prepare_submission_fence(self.root, sid, last["delivery_id"],
                            claim_owner=last["claim_owner"],
                            expected_identity=pending.submission_fence_identity(last, sid))
                    pending.mark_sent_ambiguous(self.root, sid, record["delivery_id"], claim_owner=record["claim_owner"])
                    self.store.update(duty["id"], observation={**self.store.get(duty["id"])["observation"], "transfer_ref": ref})
                    path = pending.record_path(self.root, sid, record["delivery_id"])
                    raw = json.loads(path.read_text()); raw["claim_deadline_ns"] = time.monotonic_ns() - 1
                    path.write_text(json.dumps(raw))
                tidy._append_ledger_entry(tidy.Seat("pane", "held-" + mutation),
                    dict(event="succession", source="tidy-clear", harness="claude", sid=successor,
                         **{"from": {"harness": "claude", "sid": sid}}, ts=1))
                last = max(records, key=lambda value: value["delivery_id"])
                path = pending.record_path(self.root, sid, last["delivery_id"])
                if mutation in {"queued", "unverified"}:
                    peer._save_pending(dict(peer._read_pending(ref), state=mutation))
                elif mutation == "fence":
                    raw = json.loads(path.read_text()); raw["state"] = "claimed"
                    path.write_text(json.dumps(raw))
                else:
                    raw = json.loads(path.read_text())
                    raw.update({"claim_owner": "other-carrier"} if mutation == "owner" else
                               {"route_node": "replacement-node"} if mutation == "identity" else
                               {"claim_deadline_ns": time.monotonic_ns() + 30_000_000_000})
                    path.write_text(json.dumps(raw))
                protected = path.read_bytes()
                agents = {"result": {"agents": [{"agent": "claude", "pane_id": "w1:pHeld",
                          "agent_session": {"value": successor}}]}}
                with mock.patch.object(peer_steward.subprocess, "run", return_value=SimpleNamespace(
                        returncode=0, stdout=json.dumps(agents))), \
                     mock.patch.object(peer_steward, "_resolve_target", return_value=("claude", successor)), \
                     mock.patch.object(peer_steward, "cmd_prompt") as prompt:
                    peer_steward._resume_session_notice_obligations([self.store.get(d["id"]) for d in duties], self.store)
                prompt.assert_not_called()
                self.assertEqual(path.read_bytes(), protected)
                if mutation == "live-lease":
                    # Earlier claims returned safely to pending can recover normally too.
                    raw = json.loads(path.read_text()); raw["claim_deadline_ns"] = time.monotonic_ns() - 1
                    path.write_text(json.dumps(raw))
                    sent = []
                    def courier(args, *, expected_recipient, on_prepared, producer_binding):
                        wire, new_ref = peer.prepare_peer_message(Path(args.body_file).read_text(),
                            {"harness": "runtime", "session_id": None, "name": "Hearting runtime"},
                            {"harness": "claude", "session_id": successor}, defer=True,
                            refs=args.ref, producer_binding=producer_binding)
                        on_prepared(new_ref); sent.append(new_ref)
                        self.assertEqual(peer.receive_peer_message(wire, {"harness": "claude", "session_id": successor}), 0)
                        return 0
                    with mock.patch.object(peer_steward.subprocess, "run", return_value=SimpleNamespace(
                            returncode=0, stdout=json.dumps(agents))), \
                         mock.patch.object(peer_steward, "_resolve_target", return_value=("claude", successor)), \
                         mock.patch.object(peer_steward, "cmd_prompt", side_effect=courier), \
                         mock.patch.object(pending.time, "time_ns", return_value=time.time_ns() + 1_000_000_000):
                        peer_steward._resume_session_notice_obligations([self.store.get(d["id"]) for d in duties], self.store)
                    self.assertEqual(len(sent), 1)
                    self.assertTrue(all(pending.read(self.root, sid, r["delivery_id"])["state"] == "acked" for r in records))


_LEASE_HOLDER = """\
import fcntl, os, time
mode = os.environ["CHILD_MODE"]
lease = os.open(os.environ["LEASE"], os.O_RDWR | os.O_CREAT, 0o600)
if mode == "hold":
    fcntl.flock(lease, fcntl.LOCK_EX)
elif mode == "dup":
    fcntl.flock(lease, fcntl.LOCK_EX)
    spare = os.dup(lease)
elif mode == "other":
    other = os.open(os.environ["OTHER"], os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(other, fcntl.LOCK_EX)
print("ready", flush=True)
time.sleep(60)
"""


class FirstGenerationObserverReplacementTest(unittest.TestCase):
    """Replacement of observers whose argv never advertised the lease fd."""

    RELATIVE = "utilities/codex-managed-completion.py"
    FIRST_GENERATION = ["--jobs", "J", "--sealed-batch-id", "batch-x", "--attempt-id", "att-x"]

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.data = self.base / "data"
        self.releases = self.data / "releases"
        self.releases.mkdir(parents=True)
        self.current_release = self._release("current")
        self.old_release = self._release("old")
        (self.data / "current").symlink_to(self.current_release)
        self.lease = self.base / "duty.lease"
        self.lease.write_text("", encoding="utf-8")
        self.other = self.base / "other.lease"
        self.argv = [value if value != "J" else str(self.base / "jobs.log")
                     for value in self.FIRST_GENERATION]
        self.children = []
        self.addCleanup(self._reap)

    def _release(self, name):
        root = self.releases / name
        for relative in ("core", "utilities", "tools/install"):
            (root / relative).mkdir(parents=True, exist_ok=True)
        (root / "RELEASE_VERSION").write_text(name + "\n", encoding="utf-8")
        (root / "harness-manifest.json").write_text("{}\n", encoding="utf-8")
        (root / "core/CORE.md").write_text("fixture\n", encoding="utf-8")
        for relative in ("utilities/peer_obligations.py", "utilities/peer-steward.py"):
            (root / relative).write_text("", encoding="utf-8")
        shutil.copy2(_HERE.parent / "tools/install/distribution.py",
                     root / "tools/install/distribution.py")
        (root / self.RELATIVE).write_text(_LEASE_HOLDER, encoding="utf-8")
        return root

    def _checkout(self, *, markers=True):
        root = self.base / "dev-checkout"
        (root / "utilities").mkdir(parents=True, exist_ok=True)
        (root / "core").mkdir(parents=True, exist_ok=True)
        if markers:
            (root / "utilities/peer_obligations.py").write_text("", encoding="utf-8")
            (root / "core/CORE.md").write_text("fixture\n", encoding="utf-8")
        (root / self.RELATIVE).write_text(_LEASE_HOLDER, encoding="utf-8")
        return root

    def _environment(self, mode, *, data=None, agent_home=None, fixture_home=None):
        environment = {
            "HOME": str(self.base / "home"),
            "HARNESS_DATA_ROOT": str(data or self.data),
            "AGENT_HOME": str(agent_home or self.current_release),
            "PATH": os.environ.get("PATH", ""),
            "CHILD_MODE": mode,
            "LEASE": str(self.lease),
            "OTHER": str(self.other),
        }
        if fixture_home is not None:
            # The shape of a development owner's test run: its own checkout
            # as AGENT_HOME and a throwaway HOME/XDG data root.
            del environment["HARNESS_DATA_ROOT"]
            environment["HOME"] = str(fixture_home)
            environment["XDG_DATA_HOME"] = str(fixture_home / ".local/share")
        return environment

    def _start(self, root, mode="hold", *, argv=None, data=None, agent_home=None,
               fixture_home=None):
        process = subprocess.Popen(
            [sys.executable, str(root / self.RELATIVE), *(argv or self.argv)],
            env=self._environment(mode, data=data, agent_home=agent_home,
                                  fixture_home=fixture_home),
            stdout=subprocess.PIPE, stdin=subprocess.DEVNULL, text=True)
        self.children.append(process)
        self.assertEqual(process.stdout.readline().strip(), "ready")
        return process

    def _reap(self):
        for process in self.children:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
            process.stdout.close()

    def _replace(self, process, *, variants=None, replace_checkout=True):
        required = ["--jobs", self.argv[1], "--resume-obligation", "duty"]
        with mock.patch.object(obligations, "__file__",
                               str(self.current_release / "utilities/peer_obligations.py")), \
             mock.patch.dict(os.environ, self._environment("hold"), clear=True):
            binding = obligations.managed_service_binding(self.current_release)
            self.assertIsNotNone(binding)
            return obligations.replace_managed_observer(
                process.pid, obligations._proc_start_identity(process.pid), self.lease,
                self.RELATIVE, required, binding, lock_fd_option="--obligation-lock-fd",
                exact_arguments=True,
                argument_variants=[self.argv] if variants is None else variants,
                root=self.current_release, replace_checkout=replace_checkout)

    @unittest.skipUnless(_PIDFD_HANDOFF, "pidfd handoff unavailable")
    def test_lock_fd_is_found_from_proc_when_argv_does_not_advertise_it(self):
        process = self._start(self.old_release)
        self.assertEqual(self._replace(process), "replaced")
        process.wait(timeout=5)

    @unittest.skipUnless(_PIDFD_HANDOFF, "pidfd handoff unavailable")
    def test_unlisted_first_generation_arguments_are_not_replaced(self):
        process = self._start(self.old_release)
        self.assertFalse(self._replace(process, variants=[]))
        self.assertFalse(self._replace(process, variants=[[*self.argv, "--extra"]]))
        self.assertIsNone(process.poll())

    @unittest.skipUnless(_PIDFD_HANDOFF, "pidfd handoff unavailable")
    def test_no_lock_fd_proof_leaves_the_observer_alone(self):
        for mode in ("open", "other", "dup"):
            with self.subTest(mode=mode):
                process = self._start(self.old_release, mode)
                self.assertFalse(self._replace(process))
                self.assertIsNone(process.poll())

    @unittest.skipUnless(_PIDFD_HANDOFF, "pidfd handoff unavailable")
    def test_development_checkout_observer_is_legacy_and_replaced(self):
        process = self._start(self._checkout())
        self.assertEqual(self._replace(process), "replaced")
        process.wait(timeout=5)

    @unittest.skipUnless(_PIDFD_HANDOFF, "pidfd handoff unavailable")
    def test_development_checkout_with_its_own_home_is_replaced(self):
        checkout = self._checkout()
        process = self._start(checkout, agent_home=checkout,
                              fixture_home=self.base / "fixture-home")
        self.assertEqual(self._replace(process), "replaced")
        process.wait(timeout=5)

    @unittest.skipUnless(_PIDFD_HANDOFF, "pidfd handoff unavailable")
    def test_development_checkout_needs_markers_and_no_foreign_install(self):
        marker_less = self._start(self._checkout(markers=False))
        self.assertFalse(self._replace(marker_less))
        self.assertIsNone(marker_less.poll())
        marker_less.kill()
        marker_less.wait(timeout=5)
        foreign = self.base / "foreign-checkout"
        foreign.mkdir()
        elsewhere = self._start(self._checkout(), agent_home=foreign)
        self.assertFalse(self._replace(elsewhere))
        self.assertIsNone(elsewhere.poll())

    @unittest.skipUnless(_PIDFD_HANDOFF, "pidfd handoff unavailable")
    def test_development_checkout_is_left_alone_without_the_caller_opt_in(self):
        checkout = self._checkout()
        process = self._start(checkout, agent_home=checkout,
                              fixture_home=self.base / "fixture-home")
        self.assertFalse(self._replace(process, replace_checkout=False))
        self.assertIsNone(process.poll())

    @unittest.skipUnless(_PIDFD_HANDOFF, "pidfd handoff unavailable")
    def test_another_installs_release_is_not_a_checkout(self):
        foreign_data = self.base / "foreign-data"
        (foreign_data / "releases").mkdir(parents=True)
        foreign = foreign_data / "releases" / "foreign"
        shutil.copytree(self.old_release, foreign)
        process = self._start(foreign, agent_home=foreign,
                              fixture_home=self.base / "fixture-home")
        self.assertFalse(self._replace(process))
        self.assertIsNone(process.poll())

    @unittest.skipUnless(_PIDFD_HANDOFF, "pidfd handoff unavailable")
    def test_current_release_observer_is_still_reported_current(self):
        process = self._start(self.current_release)
        self.assertEqual(self._replace(process), "current")
        self.assertIsNone(process.poll())


if __name__ == "__main__":
    unittest.main()
