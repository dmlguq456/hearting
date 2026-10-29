#!/usr/bin/env python3
"""Real ordinary-runner discovery outages and Fleet duplicate observations.

All state is private. Native delivery and external recovery endpoints are fake;
locks, stores, read passes, discovery, ledger parsing and readiness threads are real.
"""
import contextlib
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import sys
import threading
from types import SimpleNamespace
import unittest
from unittest import mock

ROOT = Path(os.environ.get("AGENT_HOME", Path(__file__).resolve().parent.parent)).resolve()
sys.path[:0] = [str(ROOT / "utilities"), str(ROOT / "tools")]
spec = importlib.util.spec_from_file_location(
    "runner_observation_fixture", ROOT / "utilities/dispatch_lineage_observation.test.py")
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)
peer, steward = fixture.peer, fixture.steward
pending, handover, tidy, dispatch = fixture.pending, fixture.handover, fixture.tidy, fixture.dispatch

for module, relative in ((steward, "utilities/peer-steward.py"),
                         (handover, "utilities/dispatch_seat_handover.py"),
                         (dispatch, "tools/fleet/collectors/dispatch.py"),
                         (tidy, "utilities/session_tidy.py")):
    assert Path(module.__file__).resolve() == ROOT / relative


class RunnerDiscoveryObservationTest(peer._TmpRootMixin, unittest.TestCase):
    notice = peer.SessionNoticeIntegrationCorrectionTest.notice
    edge = peer.SessionNoticeIntegrationCorrectionTest.edge
    store = peer.SessionNoticeIntegrationCorrectionTest.store
    duties = peer.SessionNoticeIntegrationCorrectionTest.duties
    transport = peer.SessionNoticeIntegrationCorrectionTest.transport
    prepared_notice = peer.SessionNoticeIntegrationCorrectionTest.prepared_notice
    pending_message = peer.ObligationStartupTest.pending_message

    def setUp(self):
        super().setUp()
        os.environ["AGENT_HOME"] = str(ROOT)
        handover._LINEAGE_GRAPH_CACHE.clear()

    def exercise(self, failure="read-none", *, legacy=False, service=False,
                 replace_lock=False, historical=False, no_resend=None):
        inventory = failure.startswith("pending-") or failure.startswith("receipt-")
        store, message = self.pending_message()
        if historical or no_resend and no_resend != "fenced":
            notice, ref, _ = self.prepared_notice("claude", "A", "B", "runner-outage")
            transfer = steward.peer_message._read_pending(ref)
            if no_resend in {"queued", "unverified"}:
                steward.peer_message._save_pending(dict(transfer, state=no_resend))
            elif no_resend == "missing":
                steward.peer_message._pending_path(ref).unlink()
        else:
            with contextlib.ExitStack() as stack:
                if legacy:
                    stack.enter_context(mock.patch.object(
                        steward.peer_obligations, "retain_session_notice", return_value=False))
                notice = self.notice(key="runner-outage")
            self.edge("claude", "A", "B")
            if no_resend == "fenced":
                claim = pending.claim(self.tmp_root, "A", notice["delivery_id"],
                    claim_owner="old-native-queue", lease_seconds=1,
                    require_generation_proof=False, jobs=self.jobs_path)
                pending.prepare_submission_fence(self.tmp_root, "A", notice["delivery_id"],
                    claim_owner=claim["claim_owner"],
                    expected_identity=pending.submission_fence_identity(claim, "B"))
        self.assertEqual(len(self.duties()), 0 if legacy else 1)
        other = tidy.Seat("pane", "unrelated-valid-seat")
        tidy._append_ledger_entry(other, dict(event="start", harness="claude", sid="other"))
        failed_path = tidy._ledger_path(other)
        directory = failed_path.parent
        ledger_before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in directory.iterdir()}
        broken_path = None
        if inventory:
            # Another candidate can be unavailable while A's accepted notice
            # still progresses. Legacy recovery instead fails A's only copy.
            import session_notice
            with mock.patch.object(steward.peer_obligations, "retain_session_notice", return_value=False):
                other_notice = peer._legacy_notice("claude", "other", key="inventory-other",
                    subject="fixture", text="private", jobs=self.jobs_path)
            broken_path = pending.record_path(self.tmp_root, "other", other_notice["delivery_id"])
            broken_path.write_text(json.dumps(dict(other_notice, receipt={})))
            failed_path = (pending.record_path(self.tmp_root, "A", notice["delivery_id"])
                           if legacy else broken_path)
            directory = failed_path.parent
        observed_path_before = ((failed_path.read_bytes(), failed_path.stat().st_mtime_ns)
                                if inventory else None)
        notice_before = pending.read(self.tmp_root, "A", notice["delivery_id"])
        identity = pending.received_ack_identity(notice_before, "A", notice["delivery_id"])
        handover._LINEAGE_GRAPH_CACHE.clear()  # Cold graph; writer retention used the healthy generation.
        unavailable = True
        ordinary_passes, discoveries, messages, delays, workers, contexts = [], [], [], [], [], []
        prepared_messages = []
        watch_calls, batch_calls, external_passes, read_contexts = [], [], [], []
        started = threading.Event()
        real_read, real_stat, real_lstat, real_iterdir = tidy.read_bytes, Path.stat, Path.lstat, Path.iterdir
        real_text = Path.read_text
        real_recipient = handover.record_recipient
        real_begin = steward.peer_obligations.ObligationStore.begin_read_pass
        real_discovery, real_readiness = steward._discover_session_notice_duties, steward._runner_readiness_loop
        real_reconcile = steward._reconcile_obligations
        real_resume_message = steward._resume_message_obligation
        real_create = steward.peer_obligations.ObligationStore.create
        context_type = steward.dispatch_batch_obligations.OwnerSettlementReadContext
        main_thread = threading.current_thread()
        lock_path = store.root / steward.peer_obligations.RUNNER_LOCK_NAME

        def read(path):
            if unavailable and path == failed_path:
                if failure == "read-none":
                    return None
                if failure == "truncated":
                    return b'{"event":"succession",'
                if failure == "relation":
                    return b'{"event":"succession","source":"tidy-clear","sid":"B"}\n'
            return real_read(path)

        def stat(path, *args, **kwargs):
            if unavailable and failure in {"directory-stat", "pending-directory-stat"} and path == directory:
                raise PermissionError("fixture lineage directory stat")
            if unavailable and failure == "pending-record-stat" and path == failed_path:
                raise PermissionError("fixture pending record stat")
            return real_stat(path, *args, **kwargs)

        def lstat(path, *args, **kwargs):
            if unavailable and failure == "ledger-stat" and path == failed_path:
                raise PermissionError("fixture one ledger stat")
            return real_lstat(path, *args, **kwargs)

        def iterdir(path):
            if unavailable and failure in {"enumeration", "pending-enumeration"} and path == directory:
                raise PermissionError("fixture lineage enumeration")
            return real_iterdir(path)

        def read_text(path, *args, **kwargs):
            if unavailable and inventory and path == failed_path:
                if failure == "pending-read":
                    raise PermissionError("fixture pending read")
                if failure == "pending-parse":
                    return '{"receipt":'
                if failure.startswith("receipt-"):
                    body = {"null": None, "list": [], "string": "notice", "scalar": 7, "bool": True}
                    return json.dumps(dict(json.loads(real_text(path)), receipt=body[failure[8:]]))
            return real_text(path, *args, **kwargs)

        def recipient(record, storage_sid, **kwargs):
            if unavailable and failure == "pending-unresolved" and storage_sid == "A":
                return None
            return real_recipient(record, storage_sid, **kwargs)

        def create(current, *args, **kwargs):
            if unavailable and failure == "pending-retention" and args[1] == "session-notice":
                raise OSError("fixture duty retention unavailable")
            return real_create(current, *args, **kwargs)

        def begin(current):
            real_begin(current)
            if threading.current_thread() is main_thread:
                ordinary_passes.append(current._active_list_context)

        def discover(current, **kwargs):
            observed = real_discovery(current, **kwargs)
            discoveries.append(observed)
            return observed

        def readiness(root, fd, incarnation, stop):
            workers.append((threading.current_thread(), incarnation, stop))
            started.set()
            real_readiness(root, fd, incarnation, stop)

        def make_context():
            context = context_type()
            context.close = mock.Mock(wraps=context.close)
            contexts.append(context)
            return context

        def resume_message(row, current, **kwargs):
            if row["id"] != message["id"]:
                # A prepared transfer can schedule its existing message duty.
                # Observe it through the real consumer, with native identity
                # still unknown; never fake its completion or submit it again.
                self.assertEqual(no_resend, "pending")
                prepared_messages.append(row["id"])
                ident = {"harness": "claude", "session_id": "B", "pane": "w1:pLive"}
                with mock.patch.object(steward, "_retire_target", return_value=("idle", ident, 0, "")), \
                     mock.patch.object(steward, "_prompt_current_candidate", return_value=(None, "fixture-native-unknown")):
                    real_resume_message(row, current, **kwargs)
                self.assertEqual(current.get(row["id"])["state"], "unknown")
                return
            messages.append(row["id"])
            current.update(row["id"], state="complete", cleanup="complete")

        def reconcile(current, duties, **kwargs):
            read_contexts.append(kwargs["read_context"])
            real_reconcile(current, duties, **kwargs)

        def snapshot():
            self.assertTrue(started.wait(3), "existing readiness worker started")
            external_passes.append(len(external_passes) + 1)
            active = not legacy and len(external_passes) < 4
            return {"watch_root": self.tmp_root / "watch", "jobs_stamps": {},
                    "watches": (("watch-external", {}),) if active else (),
                    "batches": {"signature": (("batch-external",),) if active else ()}}

        def service_exec(fd, root, current, **kwargs):
            self.assertTrue(all(stop.is_set() and not thread.is_alive()
                                for thread, _, stop in workers))
            contexts[-1].close.assert_called_once()
            self.assertTrue(steward._runner_lock_current(store, fd))
            self.assertEqual(kwargs["herdr_session"], "runner-endpoint")
            started.clear()
            return False  # The normal unchanged-service path returns to this runner.

        def receive_exact():
            duty = self.duties()[0]
            text = steward.peer_message._read_pending(duty["observation"]["transfer_ref"])["text"]
            steward.peer_message.receive_peer_message(text, {"harness": "claude", "session_id": "B"})

        def wait(delay):
            nonlocal unavailable
            delays.append(delay)
            self.assertLessEqual(len(delays), 3, "ordinary runner must make progress or honor replacement")
            self.assertEqual(steward._HERDR_SESSION, "runner-endpoint")
            # The same runner holds its actual lock throughout uncertain observation.
            with lock_path.open("r+") as contender:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(store.get(message["id"])["state"], "complete")
            if len(delays) <= 2:
                if inventory and legacy:
                    self.assertEqual(observed_path_before,
                                     (failed_path.read_bytes(), real_stat(failed_path).st_mtime_ns))
                if not inventory or legacy or historical or no_resend:
                    native.assert_not_called()
                else:
                    self.assertEqual(native.call_count, 1, "usable notice progresses during partial inventory")
                if not inventory:
                    self.assertEqual(handover._LINEAGE_GRAPH_CACHE, {})
            if historical and len(delays) == 1:
                receive_exact()  # Exact historical receipt settles while authority is unknown.
            if len(delays) == 2:
                if historical:
                    self.assertEqual(pending.read(self.tmp_root, "A", notice["delivery_id"])["state"], "acked")
                unavailable = False  # No ledger bytes, size or mtime change.
            if replace_lock or no_resend and len(delays) == 3:
                lock_path.unlink()  # Private replacement exercises the existing runner exit.
                lock_path.touch()
            elif len(delays) == 3:
                if historical:
                    self.assertFalse(self.duties(), "exact receipt is already settled; only external work remains")
                else:
                    receive_exact()

        try:
            with lock_path.open("w") as lock, self.transport("B") as native, contextlib.ExitStack() as stack:
                for patch in (
                    mock.patch.object(tidy, "read_bytes", side_effect=read),
                    mock.patch.object(Path, "stat", new=stat),
                    mock.patch.object(Path, "lstat", new=lstat),
                    mock.patch.object(Path, "iterdir", new=iterdir),
                    mock.patch.object(Path, "read_text", new=read_text),
                    mock.patch.object(handover, "record_recipient", side_effect=recipient),
                    mock.patch.object(steward.peer_obligations.ObligationStore, "create", new=create),
                    mock.patch.object(steward.peer_obligations.ObligationStore, "begin_read_pass", new=begin),
                    mock.patch.object(steward, "_discover_session_notice_duties", side_effect=discover),
                    mock.patch.object(steward, "_reconcile_obligations", side_effect=reconcile),
                    mock.patch.object(steward, "_runner_readiness_loop", side_effect=readiness),
                    mock.patch.object(steward, "_resume_message_obligation", side_effect=resume_message),
                    mock.patch.object(steward, "_external_obligation_snapshot", side_effect=snapshot),
                    mock.patch.object(steward, "_external_obligation_snapshot_unchanged", return_value=True),
                    mock.patch.object(steward, "_ensure_watch_observers", side_effect=lambda **kw: watch_calls.append(kw)),
                    mock.patch.object(steward.dispatch_batch_obligations, "ensure_observers",
                                      side_effect=lambda **kw: batch_calls.append(kw)),
                    mock.patch.object(steward.dispatch_batch_obligations, "OwnerSettlementReadContext",
                                      side_effect=make_context),
                    mock.patch.object(steward, "_publish_watch_readiness"),
                    mock.patch.object(steward, "_runner_readiness_fds", return_value=[]),
                    mock.patch.object(steward, "_HERDR_SESSION", "runner-endpoint"),
                    mock.patch.object(steward.time, "sleep", side_effect=wait),
                ):
                    stack.enter_context(patch)
                execute = stack.enter_context(mock.patch.object(
                    steward.peer_obligations, "exec_service_runner", side_effect=service_exec))
                fcntl.flock(lock, fcntl.LOCK_EX)
                self.assertEqual(steward.cmd_obligation_runner(SimpleNamespace(
                    state_root=self.tmp_root, lock_fd=lock.fileno(),
                    service_current="/fixture/current" if service else None)), 0)
                self.assertEqual(native.call_count, 0 if historical or no_resend or replace_lock else 1)
                self.assertEqual(execute.call_count, len(discoveries) if service else 0)
                self.assertTrue(all(not thread.is_alive() and stop.is_set() for thread, _, stop in workers))
                with lock_path.open("r+") as contender:
                    fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            # Test-only cleanup if the subject aborts; the assertions above still fail.
            for thread, _, stop in workers:
                stop.set()
                thread.join(3)
        self.assertEqual(messages, [message["id"]])
        self.assertEqual(discoveries[:1 if replace_lock else 2], [False] * (1 if replace_lock else 2))
        message_progress = inventory and no_resend == "pending"
        expected_delays = [1.0] if replace_lock else [1.0, 1.0 if historical or message_progress else 1.7]
        self.assertEqual(delays[:len(expected_delays)], expected_delays)
        if message_progress:
            self.assertTrue(prepared_messages)
            self.assertEqual(steward.peer_message._read_pending(ref)["state"], "pending")
        self.assertEqual(len(ordinary_passes), len(discoveries))
        self.assertEqual(len({id(context) for context in ordinary_passes}), len(discoveries))
        self.assertEqual(len({id(context) for context in read_contexts}), len(discoveries))
        self.assertEqual(len(batch_calls), len(discoveries))
        self.assertEqual(len(watch_calls), min(3, len(discoveries)) if not legacy else 0)
        self.assertEqual(len(workers), len(discoveries) + 1 if service else 1)
        self.assertEqual(len({incarnation for _, incarnation, _ in workers}), 1)
        marker = json.loads((store.root / "readiness-v1/runner.json").read_text())
        self.assertEqual(marker["incarnation"], workers[0][1])
        self.assertEqual(marker["pid"], os.getpid())
        for context in contexts:
            context.close.assert_called_once()
        self.assertEqual(ledger_before, {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in ledger_before})
        if inventory and not legacy:
            self.assertEqual(observed_path_before,
                             (failed_path.read_bytes(), failed_path.stat().st_mtime_ns),
                             "unrelated failed observations are read-only")
        final = pending.read(self.tmp_root, "A", notice["delivery_id"])
        self.assertEqual(pending.received_ack_identity(final, "A", notice["delivery_id"]), identity)
        if replace_lock or no_resend:
            self.assertEqual(final, notice_before)
            if legacy:
                self.assertFalse(self.duties(), "legacy record is preserved before duty recovery")
            else:
                self.assertTrue(self.duties(), "replacement never settles the retained notice")
        else:
            self.assertEqual(final["state"], "acked")
            self.assertFalse(self.duties())
            self.assertTrue(all(discoveries[2:]))
        print(json.dumps(dict(failure=failure, legacy=legacy, service=service, historical=historical,
            no_resend=no_resend, replace_lock=replace_lock, discovery=discoveries, delays=delays,
            ordinary_passes=len(ordinary_passes), readiness_workers=len(workers),
            external_passes=len(external_passes), ledger_unchanged=True, final_state=final["state"])))

    def test_read_none_among_usable_ledgers(self):
        self.exercise()

    def test_truncated_relation_among_usable_ledgers(self):
        self.exercise("truncated")

    def test_malformed_relation_among_usable_ledgers(self):
        self.exercise("relation")

    def test_directory_stat_failure(self):
        self.exercise("directory-stat")

    def test_one_ledger_stat_failure(self):
        self.exercise("ledger-stat")

    def test_directory_enumeration_failure(self):
        self.exercise("enumeration")

    def test_unknown_discovery_keeps_legacy_notice_without_a_retained_duty(self):
        self.exercise(legacy=True)

    def test_service_replacement_joins_readiness_and_closes_contexts(self):
        self.exercise(service=True)

    def test_replaced_runner_lock_stops_during_unknown_discovery(self):
        self.exercise(replace_lock=True)

    def test_historical_exact_receipt_settles_during_discovery_outage(self):
        self.exercise(historical=True)

    def test_prepared_notice_is_not_resent(self):
        self.exercise(no_resend="pending")

    def test_queued_notice_is_not_resent(self):
        self.exercise(no_resend="queued")

    def test_ambiguous_notice_is_not_resent(self):
        self.exercise(no_resend="unverified")

    def test_missing_prepared_notice_is_not_resent(self):
        self.exercise(no_resend="missing")

    def test_fenced_notice_is_not_resent(self):
        self.exercise(no_resend="fenced")

    def test_pending_directory_stat_keeps_useful_work(self):
        self.exercise("pending-directory-stat")

    def test_pending_record_stat_keeps_useful_work(self):
        self.exercise("pending-record-stat")

    def test_pending_enumeration_keeps_useful_work(self):
        self.exercise("pending-enumeration")

    def test_pending_read_keeps_useful_work(self):
        self.exercise("pending-read")

    def test_pending_parse_keeps_useful_work(self):
        self.exercise("pending-parse")

    def test_null_receipt_keeps_useful_work(self):
        self.exercise("receipt-null")

    def test_list_receipt_keeps_useful_work(self):
        self.exercise("receipt-list")

    def test_string_receipt_keeps_useful_work(self):
        self.exercise("receipt-string")

    def test_scalar_receipt_keeps_useful_work(self):
        self.exercise("receipt-scalar")

    def test_bool_receipt_keeps_useful_work(self):
        self.exercise("receipt-bool")

    def test_partial_legacy_inventory_waits_for_same_bytes_recovery(self):
        self.exercise("pending-read", legacy=True)

    def test_unresolved_legacy_recipient_keeps_existing_wait_and_authority(self):
        self.exercise("pending-unresolved", legacy=True)

    def test_failed_legacy_retention_keeps_existing_owner_until_recovery(self):
        self.exercise("pending-retention", legacy=True)

    def test_partial_inventory_service_replacement_closes_worker_and_context(self):
        self.exercise("pending-read", service=True)

    def test_partial_inventory_replaced_lock_preserves_work(self):
        self.exercise("pending-enumeration", legacy=True, replace_lock=True)

    def test_partial_inventory_historical_receipt_still_settles_exactly(self):
        self.exercise("pending-read", historical=True)

    def test_partial_inventory_prepared_notice_is_not_resent(self):
        self.exercise("pending-read", no_resend="pending")

    def test_partial_inventory_queued_notice_is_not_resent(self):
        self.exercise("pending-read", no_resend="queued")

    def test_partial_inventory_unverified_notice_is_not_resent(self):
        self.exercise("pending-read", no_resend="unverified")

    def test_partial_inventory_missing_notice_is_not_resent(self):
        self.exercise("pending-read", no_resend="missing")

    def test_partial_inventory_fenced_notice_is_not_resent(self):
        self.exercise("pending-read", no_resend="fenced")


class ScannerInventoryObservationTest(peer._TmpRootMixin, unittest.TestCase):
    notice = RunnerDiscoveryObservationTest.notice
    edge = RunnerDiscoveryObservationTest.edge
    store = RunnerDiscoveryObservationTest.store
    duties = RunnerDiscoveryObservationTest.duties

    def setUp(self):
        super().setUp()
        os.environ["AGENT_HOME"] = str(ROOT)
        handover._LINEAGE_GRAPH_CACHE.clear()

    def legacy_notice(self, sid="A"):
        with mock.patch.object(steward.peer_obligations, "retain_session_notice", return_value=False):
            return self.notice(sid=sid, key="inventory-" + sid)

    def snapshot(self, paths):
        return {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in paths}

    def test_bad_records_before_and_after_valid_notice_do_not_abort_or_prove_empty(self):
        record = self.legacy_notice()
        seat = self.edge("claude", "A", "B")
        good = pending.record_path(self.tmp_root, "A", record["delivery_id"])
        others = [good.parent / ("delivery-" + key + ".json") for key in ("000", "zzz")]
        for path in others:
            path.write_text(json.dumps(dict(record, delivery_id=path.stem, receipt={})))
        paths = [good, *others, tidy._ledger_path(seat), self.jobs_path]
        before = self.snapshot(paths)
        real_read = Path.read_text
        variants = [dict(record, receipt=body) for body in (None, [], "notice", 1, True)]
        variants += [dict(record, **{key: value}) for key, value in (
            ("receipt", {"kind": "notice"}), ("receipt", {"kind": []}),
            ("state", []), ("state", {}), ("recipient_kind", []),
            ("receipt_digest", "wrong"), (pending.SUBMISSION_FENCE_FIELD, None),
            (pending.SUBMISSION_FENCE_FIELD, []), (pending.COURIER_RETRY_FIELD, []),
            (pending.COURIER_RETRY_FIELD, {"context_digest": []}))]
        variants += [{k: v for k, v in record.items() if k != "receipt"}, [], None, "truncated"]
        for bad_path in others:
            for index, body in enumerate(variants):
                with self.subTest(position=bad_path.name, variant=index):
                    touched = []
                    def read(path, *args, **kwargs):
                        if path in [good, *others]:
                            touched.append(path)
                        if path == bad_path:
                            return '{"receipt":' if body == "truncated" else json.dumps(body)
                        return real_read(path, *args, **kwargs)
                    with mock.patch.object(Path, "read_text", new=read):
                        self.assertFalse(steward._discover_session_notice_duties(self.store()))
                    self.assertEqual(set(touched), set([good, *others]))
                    self.assertEqual(len(self.duties()), 1, "valid legacy notice retained despite other input")
                    self.assertEqual(self.duties()[0]["intent"]["identity"]["delivery_id"], record["delivery_id"])
                    self.assertEqual(before, self.snapshot(paths))
        self.assertTrue(steward._discover_session_notice_duties(self.store()))

    def test_legacy_nonnotice_terminal_and_claim_fields_keep_existing_formats(self):
        record = self.legacy_notice()
        self.edge("claude", "A", "B")
        path = pending.record_path(self.tmp_root, "A", record["delivery_id"])
        real_read = Path.read_text
        cases = [dict(record, receipt=body) for body in ({}, {"kind": "completion"}, {"result": "PASS"})]
        cases += [dict(record, state="acked", receipt=None, claim_authority="deliverer-unproven")]
        cases += [{k: dict(record, receipt={})[k] for k in pending.REQUIRED_FIELDS
                   if k != "claim_authority"}]
        # The current pending format permits these claim fields. Discovery does
        # not invent a stricter schema or use them as native-send authority.
        cases += [dict(record, claim_owner=[], claim_deadline_ns={})]
        before = self.snapshot([path, self.jobs_path])
        for index, body in enumerate(cases):
            with self.subTest(case=index), mock.patch.object(Path, "read_text", new=lambda p,*a,**kw:
                    json.dumps(body) if p == path else real_read(p,*a,**kw)):
                self.assertTrue(steward._discover_session_notice_duties(self.store()))
        self.assertEqual(before, self.snapshot([path, self.jobs_path]))

    def test_partial_enumeration_keeps_already_yielded_usable_candidates(self):
        record = self.legacy_notice()
        self.edge("claude", "A", "B")
        path = pending.record_path(self.tmp_root, "A", record["delivery_id"])
        real_iterdir = Path.iterdir
        def iterdir(directory):
            if directory == path.parent:
                def partial():
                    yield path
                    raise PermissionError("fixture late enumeration failure")
                return partial()
            return real_iterdir(directory)
        with mock.patch.object(Path, "iterdir", new=iterdir):
            self.assertFalse(steward._discover_session_notice_duties(self.store()))
        self.assertEqual(len(self.duties()), 1)

    def test_registry_failure_keeps_graph_candidates_and_recovers_same_generation(self):
        import dispatch_registry_cache as cache
        record = self.legacy_notice()
        self.edge("claude", "A", "B")
        other = self.legacy_notice("registry-only")
        self.jobs_path.write_text("now\trunning\t/w\t/w\tfixture\tattempt_id=fixture,parent_sid=registry-only,parent_harness=claude\n")
        paths = [self.jobs_path, pending.record_path(self.tmp_root, "A", record["delivery_id"]),
                 pending.record_path(self.tmp_root, "registry-only", other["delivery_id"])]
        before = self.snapshot(paths)
        real_stat = Path.stat
        def stat(path, *a, **kw):
            if path == self.jobs_path:
                raise PermissionError("fixture registry stat")
            return real_stat(path, *a, **kw)
        cache.clear_cache()
        with mock.patch.object(Path, "stat", new=stat):
            self.assertFalse(steward._discover_session_notice_duties(self.store()))
        self.assertTrue(steward._discover_session_notice_duties(self.store()))
        self.assertEqual({d["intent"]["identity"]["storage_sid"] for d in self.duties()}, {"A", "registry-only"})
        self.assertEqual(before, self.snapshot(paths))

    def test_partial_registry_text_preserves_usable_rows(self):
        record = self.legacy_notice("registry-only")
        row = b"now\trunning\t/w\t/w\tfixture\tattempt_id=fixture,parent_sid=registry-only,parent_harness=claude\n"
        for suffix in (b"partial", b"bad\n", b"\xff\n"):
            with self.subTest(suffix=suffix):
                self.jobs_path.write_bytes(row + suffix)
                before = self.snapshot([self.jobs_path])
                self.assertFalse(steward._discover_session_notice_duties(self.store()))
                self.assertEqual(len(self.duties()), 1)
                self.assertEqual(before, self.snapshot([self.jobs_path]))

    def test_absent_registry_does_not_block_valid_route_free_notice(self):
        self.legacy_notice()
        self.edge("claude", "A", "B")
        self.jobs_path.unlink()
        self.assertTrue(steward._discover_session_notice_duties(self.store()))
        self.assertEqual(len(self.duties()), 1)

    def test_real_unresolved_lineage_is_distinct_from_absent_notice(self):
        record = self.legacy_notice()
        self.edge("claude", "A", "B")
        self.edge("claude", "A", "C")
        self.assertEqual(handover.chain_end("claude", "A")[1], "unresolved")
        path = pending.record_path(self.tmp_root, "A", record["delivery_id"])
        before = self.snapshot([path, self.jobs_path])
        self.assertFalse(steward._discover_session_notice_duties(self.store()))
        self.assertFalse(self.duties())
        self.assertEqual(before, self.snapshot([path, self.jobs_path]))

    def test_retention_recheck_cannot_drop_newly_unresolved_authority(self):
        record = self.legacy_notice()
        self.edge("claude", "A", "B")
        path = pending.record_path(self.tmp_root, "A", record["delivery_id"])
        before = self.snapshot([path, self.jobs_path])
        retain = steward.peer_obligations.retain_session_notice
        fork = tidy.state_root() / "sessions" / "retention-race.jsonl"
        def retain_after_fork(*args, **kwargs):
            # Discovery resolved A already; change the real ledger before the
            # retention effect rechecks it, rather than intercepting an old reader.
            fork.write_text(json.dumps(dict(event="succession", source="tidy-clear",
                harness="claude", sid="C", **{"from": "A"})) + "\n")
            return retain(*args, **kwargs)
        with mock.patch.object(steward.peer_obligations, "retain_session_notice",
                               side_effect=retain_after_fork) as retained:
            self.assertFalse(steward._discover_session_notice_duties(self.store()))
        retained.assert_called_once()
        self.assertFalse(self.duties())
        self.assertEqual(before, self.snapshot([path, self.jobs_path]))
        fork.unlink()  # Retire only the fixture's injected competing edge.
        self.assertTrue(steward._discover_session_notice_duties(self.store()))
        self.assertEqual(len(self.duties()), 1)

    def test_registry_read_failure_still_retains_route_free_candidates(self):
        import dispatch_registry_cache as cache
        self.legacy_notice()
        self.edge("claude", "A", "B")
        cache.clear_cache()
        with mock.patch.object(cache, "_stable_read", side_effect=OSError("fixture registry read")):
            self.assertFalse(steward._discover_session_notice_duties(self.store()))
        self.assertEqual(len(self.duties()), 1)
        self.assertTrue(steward._discover_session_notice_duties(self.store()))

    def test_missing_record_during_stat_or_read_is_ordinary_absence(self):
        record = self.legacy_notice()
        self.edge("claude", "A", "B")
        path = pending.record_path(self.tmp_root, "A", record["delivery_id"])
        before = self.snapshot([path, self.jobs_path])
        real_stat, real_read = Path.stat, Path.read_text
        for operation, original in (("stat", real_stat), ("read_text", real_read)):
            with self.subTest(operation=operation):
                def missing(p, *a, **kw):
                    if p == path:
                        raise FileNotFoundError("fixture concurrent removal")
                    return original(p, *a, **kw)
                with mock.patch.object(Path, operation, new=missing):
                    self.assertTrue(steward._discover_session_notice_duties(self.store()))
                self.assertFalse(self.duties())
        self.assertEqual(before, self.snapshot([path, self.jobs_path]))
        self.assertTrue(steward._discover_session_notice_duties(self.store()))
        self.assertEqual(len(self.duties()), 1)

    def test_nonfiles_and_empty_record_are_partial_without_losing_valid_notice(self):
        record = self.legacy_notice()
        self.edge("claude", "A", "B")
        path = pending.record_path(self.tmp_root, "A", record["delivery_id"])
        bad = path.parent/"delivery-000.json"
        bad.mkdir()
        self.assertFalse(steward._discover_session_notice_duties(self.store()))
        self.assertEqual(len(self.duties()), 1)
        bad.rmdir()
        bad.touch()  # Empty JSON is unavailable, whereas an empty directory is healthy.
        self.assertFalse(steward._discover_session_notice_duties(self.store()))
        self.assertEqual(len(self.duties()), 1)
        bad.unlink()
        tidy._append_ledger_entry(tidy.Seat("pane", "not-directory"),
                                 dict(event="start", harness="claude", sid="not-directory"))
        pending.record_directory(self.tmp_root, "not-directory").touch()
        self.assertFalse(steward._discover_session_notice_duties(self.store()))
        self.assertEqual(len(self.duties()), 1)

    def test_healthy_missing_and_empty_pending_inventory_normally_exit(self):
        store = self.store()
        tidy._append_ledger_entry(tidy.Seat("pane", "empty"), dict(event="start", harness="claude", sid="empty"))
        directory = pending.record_directory(self.tmp_root, "empty")
        for present in (False, True):
            if present:
                directory.mkdir(parents=True)
            with self.subTest(present=present), contextlib.ExitStack() as stack:
                for patch in (mock.patch.object(steward, "_runner_readiness_loop"),
                        mock.patch.object(steward, "_external_obligation_snapshot", return_value={}),
                        mock.patch.object(steward, "_external_obligation_signature", return_value=()),
                        mock.patch.object(steward, "_external_obligation_snapshot_unchanged", return_value=True),
                        mock.patch.object(steward, "_reconcile_external_obligations"),
                        mock.patch.object(steward, "_publish_watch_readiness")):
                    stack.enter_context(patch)
                sleep = stack.enter_context(mock.patch.object(steward.time, "sleep"))
                self.assertTrue(steward._discover_session_notice_duties(store))
                with (store.root/steward.peer_obligations.RUNNER_LOCK_NAME).open("w") as lock:
                    fcntl.flock(lock, fcntl.LOCK_EX)
                    self.assertEqual(steward.cmd_obligation_runner(SimpleNamespace(
                        state_root=self.tmp_root, lock_fd=lock.fileno())), 0)
                sleep.assert_not_called()


class FleetDuplicateObservationTest(peer._TmpRootMixin, unittest.TestCase):
    notice = fixture.ObservationTest.notice
    edge = fixture.ObservationTest.edge
    handoff = fixture.ObservationTest.handoff
    registered_record = fixture.ObservationTest.registered_record
    snapshot = fixture.ObservationTest.snapshot

    def setUp(self):
        super().setUp()
        os.environ["AGENT_HOME"] = str(ROOT)
        handover._LINEAGE_GRAPH_CACHE.clear()

    def assert_both_orders(self, record, storage, duplicate, expected, *, live=()):
        first = pending.record_path(self.tmp_root, storage, record["delivery_id"])
        other = self.tmp_root / "second-root"
        other.mkdir()
        jobs = other / "jobs.log"
        jobs.touch()
        second = pending.record_path(other, storage, record["delivery_id"])
        second.parent.mkdir(parents=True)
        second.write_text(json.dumps(duplicate))
        expired = first.parent / "legacy-expired.json"
        expired.write_text('{"state":"expired"}')
        retained = other / "supervisor-state/obligations/batch-retained.json"
        retained.parent.mkdir(parents=True)
        retained.write_text('{"state":"unknown"}')
        paths = (first, second, expired, retained, self.jobs_path, jobs)
        before = {p: self.snapshot(p) for p in paths}
        results = []
        for roots in ([self.jobs_path, jobs], [jobs, self.jobs_path]):
            counts = dispatch._pending_delivery_counts([str(p) for p in roots], session_observation={
                "complete": True, "herdr_complete": True, "process_complete": True,
                "native_sid_complete": True, "live": set(live)})
            self.assertEqual(counts, dict(pending=2, expired=1, retained_batch_duties=1,
                                          no_recipient=expected))
            results.append(counts)
        self.assertEqual(before, {p: self.snapshot(p) for p in paths})
        print(json.dumps(dict(orders=results, bytes_mtime_unchanged=True)))

    def dead_notice(self):
        record = self.notice(sid="dead-A")
        tidy._append_ledger_entry(tidy.Seat("pane", "dead-A"),
                                 dict(event="start", harness="claude", sid="dead-A"))
        return record

    def test_unresolved_duplicate_body_is_unknown_in_both_orders(self):
        record = self.dead_notice()
        duplicate = dict(record, receipt=None)
        self.assertIsNone(handover.record_recipient_binding(duplicate, "dead-A", harness="claude"))
        self.assert_both_orders(record, "dead-A", duplicate, None)

    def test_malformed_notice_body_is_unknown_in_both_orders(self):
        record = self.dead_notice()
        duplicate = dict(record, receipt={"kind": "notice"})
        self.assertIsNone(handover.record_recipient_binding(duplicate, "dead-A", harness="claude"))
        self.assert_both_orders(record, "dead-A", duplicate, None)

    def test_valid_identical_dead_duplicates_count_once(self):
        record = self.dead_notice()
        self.assert_both_orders(record, "dead-A", record, 1)

    def test_valid_identical_live_duplicates_keep_known_zero(self):
        record = self.dead_notice()
        self.assert_both_orders(record, "dead-A", record, 0, live={('claude', 'dead-A')})

    def conflicting_registered(self, *, different_harness):
        first, record, _ = self.registered_record("duplicate", "codex")
        second = dict(first, attempt_id="att-" + "e" * 32, route_id="rt-" + "e" * 16)
        with self.jobs_path.open("a") as stream:
            stream.write("now\trunning\t/w\t/w\ttask\t" +
                         ",".join(k + "=" + v for k, v in second.items()) + "\n")
        target_harness = "opencode" if different_harness else "codex"
        target_sid = "duplicate-Z" if different_harness else "different-holder"
        tidy._append_ledger_entry(tidy.Seat("pane", "different-binding"), dict(
            event="handover", source="retire", harness=target_harness, sid=target_sid,
            **{"from": "duplicate-B"}, ts=3, bindings=[handover.binding_of(self.jobs_path, second)]))
        duplicate = dict(record, attempt_ids=[second["attempt_id"]])
        self.assertEqual(handover.record_recipient_binding(record, "duplicate-A", harness="claude"),
                         ("codex", "duplicate-Z"))
        self.assertEqual(handover.record_recipient_binding(duplicate, "duplicate-A", harness="claude"),
                         (target_harness, target_sid))
        return first, second, record, duplicate

    def test_distinct_valid_sid_bindings_for_one_identity_stay_unknown(self):
        _, _, record, duplicate = self.conflicting_registered(different_harness=False)
        self.assert_both_orders(record, "duplicate-A", duplicate, None,
                                live={("codex", "duplicate-Z")})

    def test_same_sid_on_distinct_valid_harnesses_stays_unknown(self):
        _, _, record, duplicate = self.conflicting_registered(different_harness=True)
        self.assert_both_orders(record, "duplicate-A", duplicate, None,
                                live={("codex", "duplicate-Z"), ("opencode", "duplicate-Z")})

    def test_mixed_aggregate_duplicate_stays_unknown(self):
        first, second, record, _ = self.conflicting_registered(different_harness=True)
        duplicate = dict(record, attempt_ids=[first["attempt_id"], second["attempt_id"]])
        self.assertIsNone(handover.record_recipient_binding(duplicate, "duplicate-A", harness="claude"))
        self.assert_both_orders(record, "duplicate-A", duplicate, None)


if __name__ == "__main__":
    unittest.main(verbosity=2)
