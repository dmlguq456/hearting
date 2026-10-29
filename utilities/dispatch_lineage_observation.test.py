#!/usr/bin/env python3
"""Incomplete authority observations and Fleet's current-recipient join.

Uses real ledger, pending-record, courier and preparation paths; native I/O is fake.
"""
import contextlib
from collections import Counter
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import time
from types import SimpleNamespace
import unittest
from unittest import mock

ROOT = Path(os.environ.get("AGENT_HOME", Path(__file__).resolve().parent.parent)).resolve()
sys.path[:0] = [str(ROOT / "utilities"), str(ROOT / "tools")]


def fixture(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def ledger_copies(seat, row):
    """The two canonical files written by _append_ledger_entry for this event."""
    logical = tidy.session_seat(seat, row["harness"], row["sid"])
    return tuple(dict.fromkeys((tidy._native_ledger_path(logical), tidy._ledger_path(seat))))


peer = fixture("lineage_peer_fixture", "utilities/peer_steward.test.py")
steward = peer.peer_steward
import dispatch_pending_delivery as pending
import dispatch_seat_handover as handover
import dispatch_session_sweep as sweep
import session_tidy as tidy
from peer_read_context import PeerReadContext
from fleet.collectors import dispatch

for module, relative in ((handover, "utilities/dispatch_seat_handover.py"),
                         (tidy, "utilities/session_tidy.py"),
                         (steward, "utilities/peer-steward.py"),
                         (dispatch, "tools/fleet/collectors/dispatch.py")):
    assert Path(module.__file__).resolve() == ROOT / relative


class ObservationTest(peer._TmpRootMixin, unittest.TestCase):
    notice = peer.SessionNoticeIntegrationCorrectionTest.notice
    edge = peer.SessionNoticeIntegrationCorrectionTest.edge
    store = peer.SessionNoticeIntegrationCorrectionTest.store
    duties = peer.SessionNoticeIntegrationCorrectionTest.duties
    transport = peer.SessionNoticeIntegrationCorrectionTest.transport
    prepared_notice = peer.SessionNoticeIntegrationCorrectionTest.prepared_notice
    handoff = peer.ExplicitHolderAuthorityTest.handoff
    ambiguity = peer.ExplicitHolderAuthorityTest.ambiguity

    def setUp(self):
        super().setUp()
        os.environ["AGENT_HOME"] = str(ROOT)
        handover._LINEAGE_GRAPH_CACHE.clear()

    @contextlib.contextmanager
    def unreadable(self, path, *, enabled=lambda: True):
        real_open = os.open

        def open_file(candidate, *args, **kwargs):
            if Path(candidate) == path and enabled():
                raise PermissionError("fixture ledger unreadable")
            return real_open(candidate, *args, **kwargs)

        with mock.patch.object(tidy.os, "open", side_effect=open_file):
            yield

    def counts(self, live=()):
        return dispatch._pending_delivery_counts([str(self.jobs_path)], session_observation={
            "complete": True, "herdr_complete": True, "process_complete": True,
            "native_sid_complete": True, "live": set(live)})

    def snapshot(self, path):
        return path.read_bytes(), path.stat().st_mtime_ns

    def legacy_handoff(self, tag, harness="claude", holder_harness=None):
        meta, record, seat = self.handoff(tag, harness, holder_harness)
        path = tidy._ledger_path(seat)
        row = json.loads(path.read_text())
        del row["source"]
        for copy in ledger_copies(seat, row):
            copy.write_text(json.dumps(row) + "\n")
        return meta, record, seat, row

    def test_legacy_logical_binding_does_not_require_an_audit_attempt(self):
        meta, record, seat, row = self.legacy_handoff("audit-optional", "codex", "claude")
        for binding in row["bindings"]:
            binding.pop("attempt")
        for path in ledger_copies(seat, row):
            path.write_text(json.dumps(row) + "\n")
        self.assertEqual(handover.effective_parent(meta, self.jobs_path), "audit-optional-Z")
        self.assertEqual(handover.record_recipient_binding(record, "audit-optional-A",
            harness="codex", jobs=self.jobs_path), ("claude", "audit-optional-Z"))
        self.assertIn("audit-optional-A", dict(handover.storage_recipients("audit-optional-Z", harness="claude")))
        # After the exact registry row is gone the missing audit cannot identify
        # its job. Neither historical address nor an unrelated holder is authority.
        self.jobs_path.write_text("")
        self.assertIsNone(handover.record_recipient_binding(record, "audit-optional-A",
            harness="codex", jobs=self.jobs_path))

    def test_strict_state_read_rejects_missing_sid_without_poisoning_unrelated_legacy_history(self):
        meta, _record, seat = self.handoff("empty-object")
        path = tidy._ledger_path(seat)
        original = path.read_bytes()
        path.write_bytes(original + b"{}\n")
        with self.assertRaises(ValueError):
            tidy._read_ledger_lines(seat, strict=True)
        # Forward-compatible unrelated rows grant no edge and do not erase a
        # separately confirmed qualified handover in the common graph.
        self.assertEqual(handover.effective_parent(meta, self.jobs_path), "empty-object-Z")
        self.assertFalse(handover.owns(meta, "empty-object-A", self.jobs_path))
        path.write_bytes(original)
        self.assertEqual(handover.effective_parent(meta, self.jobs_path), "empty-object-Z")

    def test_cross_harness_prior_batch_keeps_the_already_acked_original_marker(self):
        import session_notice
        import peer_obligations
        from dispatch_recipient_receipt import opencode_messages_evidence, settle_native_receipt
        old, current = "cross-prior-A", "cross-prior-B"
        with mock.patch.object(peer_obligations, "ensure_normal_runner", return_value=False), \
             mock.patch.object(peer_obligations, "request_dispatch_reconciliation", return_value=False):
            records = [session_notice.notify("claude", old, key="cross-prior-" + str(i),
                subject="cross prior", text="original body " + str(i), jobs=self.jobs_path) for i in range(2)]
        tidy._append_ledger_entry(tidy.Seat("pane", "cross-prior"), {
            "event": "handover", "source": "retire", "harness": "opencode", "sid": current,
            "from": {"harness": "claude", "sid": old}, "bindings": [], "ts": 1})
        sent = []
        for record in records:
            claim = pending.claim(self.tmp_root, old, record["delivery_id"], claim_owner="original-carrier",
                lease_seconds=30, require_generation_proof=False)
            sent.append(pending.mark_sent_ambiguous(self.tmp_root, old, claim["delivery_id"], claim_owner="original-carrier"))
        original_body = sweep.delivery_context([(self.tmp_root, [{**r, sweep.STORAGE_KEY: old} for r in sent])])
        identities = [pending.received_ack_identity(r, old, r["delivery_id"]) for r in sent]
        pending.ack_received(self.tmp_root, old, sent[1]["delivery_id"], expected_identity=identities[1],
                             acked_by="independent-carrier")
        native = [{"info": {"sessionID": current, "role": "user"},
                   "parts": [{"type": "text", "text": original_body}]}]
        expected = sweep._opencode_delivery_expectations([self.tmp_root], "opencode-turn", current, native)
        self.assertEqual(len(expected), 1)
        self.assertEqual({delivery for delivery, _identity in expected[0]["bindings"]},
                         {r["delivery_id"] for r in sent})
        self.assertEqual(expected[0]["text"], original_body)
        evidence = opencode_messages_evidence(native, current, expected)
        result = settle_native_receipt(self.tmp_root, old, "opencode", current, sent[0], evidence,
                                       acked_by="cross-prior-receipt")
        self.assertTrue(result["received"] and result["settled"])
        for record, identity in zip(sent, identities):
            stored = pending.read(self.tmp_root, old, record["delivery_id"])
            self.assertEqual((stored["state"], stored["attempts"]), ("acked", 1))
            self.assertEqual(pending.received_ack_identity(stored, old, stored["delivery_id"]), identity)

    def test_legacy_exact_binding_crosses_harness_without_parent_harness_and_reuses_context(self):
        from harness_capabilities import parent_completion
        for harness, target in (("claude", "claude"), ("codex", "codex"),
                                ("opencode", "opencode"), ("codex", "claude")):
            with self.subTest(harness=harness, target=target):
                tag = "legacy-" + harness + target
                meta, record, seat, row = self.legacy_handoff(tag, harness, target)
                del meta["parent_harness"]
                meta["parent_completion_delivery"] = parent_completion(harness)["carrier"]
                with self.jobs_path.open("a") as stream:
                    stream.write("now\trunning\t/w\t/w\ttask\t" +
                                 ",".join(k + "=" + v for k, v in meta.items()) + "\n")
                before = self.snapshot(tidy._ledger_path(seat))
                registry = self.snapshot(self.jobs_path)
                context = PeerReadContext()
                for _ in range(2):
                    self.assertEqual(handover.record_recipient_binding(record, tag + "-A",
                        harness=harness, jobs=self.jobs_path, read_context=context), (target, tag + "-Z"))
                    self.assertEqual(handover.effective_parent(meta, self.jobs_path,
                                                              read_context=context), tag + "-Z")
                    self.assertEqual(handover.effective_parent_harness(meta, self.jobs_path,
                                                                      read_context=context), target)
                    self.assertFalse(handover.owns(meta, tag + "-A", self.jobs_path, read_context=context))
                self.assertIsNone(tidy.tidy_lineage_edge(row))
                self.assertEqual(self.snapshot(self.jobs_path), registry)
                self.assertEqual(self.snapshot(tidy._ledger_path(seat)), before)

    def test_legacy_binding_has_no_unbound_edge_and_does_not_poison_unrelated_lineage(self):
        meta, record, seat, row = self.legacy_handoff("legacy-only")
        notice = self.notice(sid="legacy-only-A", key="legacy-only")
        healthy = self.notice(sid="healthy-A", key="healthy")
        self.edge("claude", "healthy-A", "healthy-B")
        context = PeerReadContext()
        self.assertEqual(handover.record_recipient(record, "legacy-only-A", harness="claude",
                                                   read_context=context), "legacy-only-Z")
        self.assertEqual(handover.record_recipient(notice, "legacy-only-A", harness="claude",
                                                   read_context=context), "legacy-only-B")
        self.assertEqual(handover.record_recipient(healthy, "healthy-A", harness="claude",
                                                   read_context=context), "healthy-B")
        # A chronological start/manual clear supplies no source-qualified edge.
        tidy._append_ledger_entry(seat, dict(event="start", harness="claude", sid="manual-next", ts=3))
        tidy._append_ledger_entry(seat, dict(event="handover", source="manual-clear",
            harness="claude", sid="manual-next", **{"from": "legacy-only-Z"}, ts=4))
        # A later exact legacy binding for another logical job cannot move this job.
        foreign = dict(row, sid="foreign-holder", ts=5,
                       **{"from": "legacy-only-Z", "bindings": [dict(row["bindings"][0], node="other-node")]})
        tidy._append_ledger_entry(seat, foreign)
        self.assertEqual(handover.record_recipient(record, "legacy-only-A", harness="claude",
                                                   read_context=context), "legacy-only-Z")
        self.assertEqual(handover.chain_end("claude", "legacy-only-Z", read_context=context),
                         ("legacy-only-Z", "self"))

    def test_record_checks_use_current_attempt_index_without_expanding_history(self):
        from dispatch_registry_cache import RegistryView
        meta, record, seat = self.handoff("indexed-record")
        missing = {"attempt_ids": ["att-unregistered"]}
        # Neither the exact job nor the legacy missing-row fallback needs the
        # global metadata mapping. Each call still observes the current view.
        with mock.patch.object(RegistryView, "handover_latest", side_effect=AssertionError("global expansion")):
            context = PeerReadContext()
            self.assertEqual(handover.record_recipient(record, "indexed-record-A", harness="claude",
                                                       read_context=context), "indexed-record-Z")
            self.assertTrue(handover.record_for_session(missing, None, session="own-key", harness="claude",
                storage_key="own-key", jobs=self.jobs_path, read_context=context))
            self.assertFalse(handover.record_for_session({"attempt_ids": [*record["attempt_ids"],
                *missing["attempt_ids"]]}, None, session="indexed-record-A", harness="claude",
                storage_key="indexed-record-A", jobs=self.jobs_path, read_context=context))
            changed = dict(meta, parent_sid="foreign-parent")
            with self.jobs_path.open("a") as stream:
                stream.write("now\trunning\t/w\t/w\ttask\t" +
                             ",".join(k + "=" + v for k, v in changed.items()) + "\n")
            self.assertIsNone(handover.record_recipient(record, "indexed-record-A", harness="claude",
                                                        read_context=context))
            self.assertFalse(handover.record_for_session(record, None, session="indexed-record-Z",
                harness="claude", storage_key="indexed-record-A", jobs=self.jobs_path, read_context=context))

    def test_legacy_holder_follows_only_confirmed_same_harness_successor_and_denies_fork(self):
        import route_authority
        meta, record, seat, row = self.legacy_handoff("legacy-next", "codex", "claude")
        context = PeerReadContext()
        self.assertEqual(handover.effective_parent(meta, self.jobs_path, read_context=context), "legacy-next-Z")
        tidy._append_ledger_entry(seat, dict(event="handover", source="clear", harness="claude",
            sid="legacy-next-Y", **{"from": "legacy-next-Z"}, ts=3))
        self.assertEqual(handover.record_recipient_binding(record, "legacy-next-A", harness="codex",
            read_context=context), ("claude", "legacy-next-Y"))
        self.assertFalse(handover.owns(meta, "legacy-next-Z", self.jobs_path, read_context=context))
        self.assertTrue(handover.owns(meta, "legacy-next-Y", self.jobs_path, read_context=context))
        tidy._append_ledger_entry(seat, dict(event="handover", source="clear", harness="claude",
            sid="legacy-next-X", **{"from": "legacy-next-Z"}, ts=4))
        self.assertIsNone(handover.record_recipient(record, "legacy-next-A", harness="codex",
                                                   read_context=context))
        for actor in ("legacy-next-A", "legacy-next-B", "legacy-next-Z", "legacy-next-Y", "legacy-next-X"):
            self.assertFalse(handover.owns(meta, actor, self.jobs_path, read_context=context))
            self.assertFalse(route_authority.decide_continuation(action="handover", jobs=self.jobs_path,
                metadata=meta, status="running", facts={"eligible": True, "actor": actor}).allowed)

    def test_malformed_legacy_bindings_remain_uncached_and_same_bytes_recover(self):
        meta, record, seat, row = self.legacy_handoff("legacy-malformed")
        path = tidy._ledger_path(seat)
        before = self.snapshot(path)
        malformed = [dict(row, bindings=value) for value in (None, [], {}, "bad", [None])]
        for key in ("jobs", "route", "hash", "node"):
            binding = dict(row["bindings"][0])
            del binding[key]
            malformed.append(dict(row, bindings=[binding]))
            malformed.append(dict(row, bindings=[dict(row["bindings"][0], **{key: None})]))
        malformed += [dict(row, source=None), dict(row, schema=2), dict(row, **{"from": ""}),
                      dict(row, harness=None), dict(row, sid=None)]
        real_read = tidy.read_bytes
        for bad in malformed:
            context = PeerReadContext()
            handover._LINEAGE_GRAPH_CACHE.clear()
            with self.subTest(row=bad), mock.patch.object(tidy, "read_bytes", side_effect=lambda candidate:
                    (json.dumps(bad) + "\n").encode() if candidate == path else real_read(candidate)):
                self.assertIsNone(handover.record_recipient(record, "legacy-malformed-A", harness="claude",
                                                            read_context=context))
                self.assertEqual(handover.effective_parent(meta, self.jobs_path, read_context=context), "")
                self.assertEqual(handover._LINEAGE_GRAPH_CACHE, {})
                self.assertEqual(context._lineage_graphs, {})
            self.assertEqual(handover.record_recipient(record, "legacy-malformed-A", harness="claude",
                                                       read_context=context), "legacy-malformed-Z")
            self.assertEqual(self.snapshot(path), before)

    def test_legacy_binding_exact_jobs_route_hash_node_never_grants_foreign_authority(self):
        meta, record, seat, row = self.legacy_handoff("legacy-exact")
        paths = ledger_copies(seat, row)
        for key in ("jobs", "route", "hash", "node"):
            with self.subTest(key=key):
                for path in paths:
                    path.write_text(json.dumps(row) + "\n")
                self.assertEqual(handover.record_recipient(record, "legacy-exact-A", harness="claude"),
                                 "legacy-exact-Z")
                foreign = dict(row, bindings=[dict(row["bindings"][0], **{key: "foreign-" + key})])
                tidy._ledger_path(seat).write_text(json.dumps(foreign) + "\n")
                # The logical copy still binds this exact job. A physical-only
                # mutation cannot revoke that independent canonical evidence.
                self.assertEqual(handover.record_recipient(record, "legacy-exact-A", harness="claude"),
                                 "legacy-exact-Z")
                for path in paths:
                    path.write_text(json.dumps(foreign) + "\n")
                self.assertEqual(handover.record_recipient(record, "legacy-exact-A", harness="claude"),
                                 "legacy-exact-B")
                self.assertFalse(handover.owns(meta, "legacy-exact-Z", self.jobs_path))

    def test_legacy_binding_historical_exact_receipt_settles_original_key_after_explicit_away(self):
        import dispatch_recipient_receipt
        meta, record, seat, row = self.legacy_handoff("legacy-receipt")
        receipt = {"schema_version": 2, "state": "delivered", "parent_attempt_id": "att-" + "b" * 32,
            "job_registry": str(self.jobs_path), "children": [{"attempt_id": meta["attempt_id"],
                "status": "done", "readiness": "ready", "required_action": "inspect-done-failure",
                "reason": "terminal-failure-or-unclosed", "harness": "claude"}]}
        stored = pending.create(root=self.tmp_root, recipient_kind="opencode-turn",
            recipient_key="legacy-receipt-A", delivery_id="delivery-" + "a" * 32,
            session_generation="", session_generation_supported="0", attempt_ids=record["attempt_ids"],
            parent_attempt_id=receipt["parent_attempt_id"], route_id=meta["route_id"], route_node="execute",
            receipt=receipt, receipt_digest=pending._canonical_receipt_digest(receipt),
            row_revisions={meta["attempt_id"]: "original"})
        identity = pending.received_ack_identity(stored, "legacy-receipt-A", stored["delivery_id"])
        claimed = pending.claim(self.tmp_root, "legacy-receipt-A", stored["delivery_id"],
            claim_owner="old-native-carrier", lease_seconds=30, require_generation_proof=False)
        text = sweep.delivery_context([(self.tmp_root, [{**claimed, sweep.STORAGE_KEY: "legacy-receipt-A"}])])
        # The receipt belongs to the exact old receiver, independently of new send authority.
        tidy._append_ledger_entry(seat, dict(row, sid="legacy-receipt-Y", ts=4,
                                           **{"from": "legacy-receipt-Z"}))
        self.assertEqual(handover.effective_parent(meta, self.jobs_path), "legacy-receipt-Y")
        messages = [{"info": {"sessionID": "legacy-receipt-Z", "role": "user"},
                     "parts": [{"type": "text", "text": text}]}]
        evidence = dispatch_recipient_receipt.opencode_messages_evidence(messages, "legacy-receipt-Z",
            [sweep._opencode_expected_message(text, [(stored["delivery_id"], identity)])])
        result = dispatch_recipient_receipt.settle_native_receipt(self.tmp_root, "legacy-receipt-A",
            "opencode", "legacy-receipt-Z", claimed, evidence, acked_by="fixture-exact-native")
        self.assertTrue(result["received"])
        self.assertTrue(result["settled"])
        final = pending.read(self.tmp_root, "legacy-receipt-A", stored["delivery_id"])
        self.assertEqual(final["state"], "acked")
        self.assertEqual(pending.received_ack_identity(final, "legacy-receipt-A", stored["delivery_id"]), identity)
        self.assertIsNone(pending.read(self.tmp_root, "legacy-receipt-Y", stored["delivery_id"]))

    def test_failed_one_of_two_ledgers_is_not_cached_and_same_context_recovers(self):
        notice = self.notice()
        path = tidy._ledger_path(self.edge("claude", "A", "B"))
        other = tidy.Seat("pane", "other-valid")
        other_row = dict(event="start", harness="claude", sid="other", ts=1)
        tidy._append_ledger_entry(other, other_row)
        # Two seats produce four files: both logical and physical inputs count.
        paths = set(ledger_copies(other, other_row)) | set(ledger_copies(
            tidy.Seat("pane", path.stem), json.loads(path.read_text())))
        before = self.snapshot(path)
        context = PeerReadContext()
        handover._LINEAGE_GRAPH_CACHE.clear()  # Cold observation after writer-side retention.
        with self.unreadable(path):
            for _ in range(2):
                self.assertIsNone(handover.record_recipient(notice, "A", harness="claude",
                                                            read_context=context))
            self.assertEqual(handover._LINEAGE_GRAPH_CACHE, {})
            self.assertEqual(context._lineage_graphs, {})
            self.assertIsNone(self.counts()["no_recipient"])
        raw_reads = []
        real_read = tidy.read_bytes
        def read(candidate):
            if Path(candidate).suffix == ".jsonl":
                raw_reads.append(candidate)
            return real_read(candidate)
        with mock.patch.object(tidy, "read_bytes", side_effect=read), \
                mock.patch.object(tidy, "_read_ledger_lines", wraps=tidy._read_ledger_lines) as reads:
            for _ in range(10):
                self.assertEqual(handover.record_recipient(notice, "A", harness="claude",
                                                           read_context=context), "B")
            self.assertEqual(Counter(raw_reads), Counter(paths))
            self.assertEqual(Counter(tidy._ledger_path(call.args[0]) for call in reads.call_args_list),
                             Counter(paths))
        self.assertEqual(self.snapshot(path), before)
        with self.transport("A") as native:
            steward._resume_session_notice_obligations(self.duties(), self.store())
        native.assert_not_called()
        with self.transport("B") as native:
            steward._resume_session_notice_obligations(self.duties(), self.store())
        native.assert_called_once()

    def test_healthy_337_ledgers_are_read_once_for_100_resolutions(self):
        notice = self.notice()
        directory = tidy.state_root() / "sessions"
        directory.mkdir(parents=True, exist_ok=True)
        for index in range(337):
            rows = [dict(event="start", harness="claude", sid=f"legacy-{index}-{n}")
                    for n in range(37)]
            rows.append(dict(event="succession", source="tidy-clear", harness="claude",
                             sid="B" if index == 0 else f"new-{index}",
                             **{"from": "A" if index == 0 else f"old-{index}"}))
            (directory / f"seat-{index}.jsonl").write_text(
                "".join(json.dumps(row) + "\n" for row in rows))
        context = PeerReadContext()
        started = time.monotonic()
        with mock.patch.object(tidy, "_read_ledger_lines", wraps=tidy._read_ledger_lines) as reads:
            for _ in range(100):
                self.assertEqual(handover.record_recipient(notice, "A", harness="claude",
                                                           read_context=context), "B")
            self.assertEqual(reads.call_count, 337)
        print(json.dumps({"ledgers": 337, "rows": 337 * 38, "resolutions": 100,
                          "reads": reads.call_count, "elapsed": time.monotonic() - started}))

    def test_warm_current_observation_invalidates_both_caches_and_same_bytes_recover(self):
        for harness, target in (("claude", "claude"), ("codex", "codex"),
                                ("opencode", "opencode"), ("claude", "codex")):
            for explicit in (False, True):
                for context_kind in ("same", "new", "none"):
                    tag = f"warm-{harness}-{target}-{explicit}-{context_kind}"
                    if explicit:
                        meta, record, seat = self.handoff(tag, harness, target)
                        expected = (target, tag + "-Z")
                    else:
                        meta = None
                        record = self.notice(harness=harness, sid=tag + "-A", key=tag)
                        seat = self.edge(harness, tag + "-A", tag + "-B")
                        expected = (harness, tag + "-B")
                    path = tidy._ledger_path(seat)
                    before = self.snapshot(path)
                    registry = self.snapshot(self.jobs_path)
                    context = PeerReadContext()

                    def resolve():
                        # This matrix observes current authority, including a
                        # same-context pass warmed before an effect. Read-only
                        # record loops reuse the observation instead.
                        try:
                            handover.lineage_graph(read_context=context)
                        except OSError:
                            return None
                        return handover.record_recipient_binding(record, tag + "-A",
                            harness=harness, jobs=self.jobs_path, read_context=context)

                    self.assertEqual(resolve(), expected)
                    if context_kind != "same":
                        context = PeerReadContext() if context_kind == "new" else None
                    real_read = tidy.read_bytes
                    for fault in ("open", "short-read", "corrupt", "permission"):
                        self.assertEqual(resolve(), expected)
                        with self.subTest(tag=tag, fault=fault), contextlib.ExitStack() as stack:
                            if fault in {"open", "permission"}:
                                if fault == "permission":
                                    mode = path.stat().st_mode & 0o777
                                    path.chmod(0)
                                    stack.callback(path.chmod, mode)
                                stack.enter_context(self.unreadable(path))
                            else:
                                raw = before[0][:-1] + (b"{" if fault == "corrupt" else b"")
                                stack.enter_context(mock.patch.object(tidy, "read_bytes",
                                    side_effect=lambda candidate: raw if candidate == path else real_read(candidate)))
                            for _ in range(2):
                                self.assertIsNone(resolve())
                            self.assertEqual(handover._LINEAGE_GRAPH_CACHE, {})
                            if context is not None:
                                self.assertEqual(context._lineage_graphs, {})
                            if meta is not None:
                                self.assertEqual(handover.effective_parent(meta, self.jobs_path,
                                    read_context=context), "")
                                for actor in (tag + "-A", tag + "-B", tag + "-Z"):
                                    self.assertFalse(handover.owns(meta, actor, self.jobs_path,
                                        read_context=context))
                        with mock.patch.object(tidy, "_read_ledger_lines", wraps=tidy._read_ledger_lines) as reads:
                            self.assertEqual(resolve(), expected)
                            parsed = reads.call_count
                            self.assertGreater(parsed, 0)
                            self.assertEqual(resolve(), expected)
                            self.assertEqual(reads.call_count, parsed)
                        self.assertEqual(self.snapshot(path), before)
                        self.assertEqual(self.snapshot(self.jobs_path), registry)

    @staticmethod
    def replace_ledger(path, old, new):
        before = path.stat()
        raw = path.read_bytes()
        changed = raw.replace(old.encode(), new.encode())
        assert raw != changed and len(raw) == len(changed)
        replacement = path.with_suffix(".replacement")
        replacement.write_bytes(changed)
        os.utime(replacement, ns=(before.st_atime_ns, before.st_mtime_ns))
        os.replace(replacement, path)
        assert path.stat().st_ino != before.st_ino
        assert path.stat().st_size == before.st_size
        assert path.stat().st_mtime_ns == before.st_mtime_ns

    def test_equal_metadata_replacement_updates_implicit_and_explicit_current_authority(self):
        for source, target in (("claude", "claude"), ("codex", "codex"),
                               ("opencode", "opencode"), ("codex", "claude")):
            for explicit in (False, True):
                tag = f"replace-{source}-{target}-{explicit}"
                if explicit:
                    meta, record, seat = self.handoff(tag, source, target)
                    old = tag + "-Z"
                else:
                    meta = None
                    record = self.notice(harness=source, sid=tag + "-A", key=tag)
                    seat = self.edge(source, tag + "-A", tag + "-B")
                    old = tag + "-B"
                path, current = tidy._ledger_path(seat), tag + "-Y"
                paths = ledger_copies(seat, json.loads(path.read_text().splitlines()[-1]))
                current_harness = target if explicit else source
                context = PeerReadContext()
                self.assertEqual(handover.record_recipient_binding(record, tag + "-A",
                    harness=source, read_context=context), (current_harness, old))
                self.replace_ledger(path, old, current)
                if not explicit:
                    # The unchanged logical relation and the new physical one
                    # form a real fork; neither old nor new may receive yet.
                    self.assertIsNone(handover.record_recipient_binding(record, tag + "-A",
                        harness=source, read_context=context))
                for copy in paths:
                    if copy != path:
                        self.replace_ledger(copy, old, current)
                for reader in (context, PeerReadContext(), None):
                    with self.subTest(source=source, target=target, explicit=explicit):
                        self.assertEqual(handover.record_recipient_binding(record, tag + "-A",
                            harness=source, read_context=reader), (current_harness, current))
                        self.assertFalse(handover.record_for_session(record, None, session=old,
                            harness=current_harness, storage_key=tag + "-A", read_context=reader))
                        if meta is not None:
                            self.assertFalse(handover.owns(meta, old, self.jobs_path, read_context=reader))
                            self.assertTrue(handover.owns(meta, current, self.jobs_path, read_context=reader))

    def test_replacement_during_warm_read_or_cold_parse_is_not_cached(self):
        notice = self.notice(sid="race-A", key="race")
        path = tidy._ledger_path(self.edge("claude", "race-A", "race-B"))
        paths = ledger_copies(tidy.Seat("pane", path.stem), json.loads(path.read_text()))
        context = PeerReadContext()
        self.assertEqual(handover.record_recipient(notice, "race-A", harness="claude",
                                                   read_context=context), "race-B")
        def observe():
            try:
                handover.lineage_graph(read_context=context)
            except OSError:
                return None
            return handover.record_recipient(notice, "race-A", harness="claude", read_context=context)
        real_read = tidy.read_bytes
        changed = False

        def read(candidate):
            nonlocal changed
            raw = real_read(candidate)
            if candidate == path and not changed:
                changed = True
                for copy in paths:
                    self.replace_ledger(copy, "race-B", "race-C")
            return raw

        with mock.patch.object(tidy, "read_bytes", side_effect=read):
            self.assertIsNone(observe())
        self.assertEqual(handover._LINEAGE_GRAPH_CACHE, {})
        self.assertEqual(context._lineage_graphs, {})
        real_parse = tidy._read_ledger_lines
        changed = False

        def parse(seat, **kwargs):
            nonlocal changed
            rows = real_parse(seat, **kwargs)
            if tidy._ledger_path(seat) == path and not changed:
                changed = True
                for copy in paths:
                    self.replace_ledger(copy, "race-C", "race-D")
            return rows

        with mock.patch.object(tidy, "_read_ledger_lines", side_effect=parse):
            self.assertIsNone(observe())
        self.assertEqual(handover._LINEAGE_GRAPH_CACHE, {})
        self.assertEqual(context._lineage_graphs, {})
        self.assertEqual(handover.record_recipient(notice, "race-A", harness="claude",
                                                   read_context=context), "race-D")

    def test_warm_fleet_and_history_are_unknown_during_outage_and_recover_read_only(self):
        notice = self.notice(sid="view-A", key="view")
        seat = self.edge("claude", "view-A", "view-B")
        for sid, ts in (("view-A", 1), ("view-B", 2)):
            tidy._append_ledger_entry(seat, dict(event="start", harness="claude", sid=sid, ts=ts))
        path = tidy._ledger_path(seat)
        hidden = tidy._ledger_path(tidy.Seat("pane", "view-hidden"))
        hidden.write_text(json.dumps(dict(event="start", harness="claude", sid="unrelated", ts=1)) + "\n")
        record_path = pending.record_path(self.tmp_root, "view-A", notice["delivery_id"])
        paths = (path, hidden, record_path, self.jobs_path)
        before = [self.snapshot(p) for p in paths]

        def history():
            with mock.patch.object(tidy, "_lineage_transcript", return_value={"path": None, "status": "missing"}):
                return tidy.seat_lineage(seat, harness="claude", sid="view-B")

        self.assertEqual(history()[0]["relation"], "confirmed")
        self.assertEqual(self.counts({("claude", "view-B")})["no_recipient"], 0)
        with self.unreadable(hidden):
            self.assertIsNone(self.counts({("claude", "view-B")})["no_recipient"])
            rows = history()
            self.assertTrue(rows)
            self.assertTrue(all(row["relation"] == "unknown" and row["predecessor"] is None for row in rows))
        self.assertEqual(history()[0]["relation"], "confirmed")
        self.assertEqual(self.counts({("claude", "view-B")})["no_recipient"], 0)
        self.assertEqual([self.snapshot(p) for p in paths], before)

    def test_fleet_record_loop_and_history_reuse_each_observation(self):
        paths = set()
        for index in range(3):
            self.notice(sid=f"fleet-{index}-A", key=f"fleet-{index}")
            seat = tidy.Seat("pane", f"fleet-seat-{index}")
            row = dict(event="succession", source="tidy-clear", harness="claude",
                       **{"from": f"fleet-{index}-A"}, sid=f"fleet-{index}-B")
            tidy._append_ledger_entry(seat, row)
            paths.update(ledger_copies(seat, row))
        real_read = tidy.read_bytes
        raw = []
        def read(path):
            data = real_read(path)
            if Path(path).suffix == ".jsonl":
                raw.append((str(path), len(data or b"")))
            return data
        with mock.patch.object(tidy, "read_bytes", side_effect=read):
            self.assertEqual(self.counts()["no_recipient"], 3)
            self.assertEqual(Counter(Path(path) for path, _size in raw), Counter(paths))
            observed = list(raw)
            seat = tidy.Seat("pane", Path(raw[0][0]).stem)
            with mock.patch.object(tidy, "_lineage_transcript", return_value={"path": None, "status": "missing"}):
                tidy.seat_lineage(seat, harness="claude")
            self.assertEqual(raw, observed + observed)

    def test_effect_lock_reobserves_authority_after_the_same_context_was_warmed(self):
        meta, record, seat = self.handoff("warm-admission", "claude", "codex")
        context = PeerReadContext()
        self.assertEqual(handover.record_recipient_binding(record, "warm-admission-A", harness="claude",
            jobs=self.jobs_path, read_context=context), ("codex", "warm-admission-Z"))
        with self.unreadable(tidy._ledger_path(seat)):
            with handover.lineage_effect_lock(meta, self.jobs_path, "warm-admission-Z") as current:
                self.assertFalse(current)
        with handover.lineage_effect_lock(meta, self.jobs_path, "warm-admission-Z") as current:
            self.assertTrue(current)

    def test_opencode_projection_uses_the_current_readable_graph_generation(self):
        seat = self.edge("opencode", "projection-A", "projection-B")
        path = tidy._ledger_path(seat)

        def project():
            output = io.StringIO()
            with mock.patch.object(handover, "pane_seat", return_value=seat), contextlib.redirect_stdout(output):
                self.assertEqual(sweep.main(["recipients", "--recipient-kind", "opencode-turn",
                                             "--session", "projection-B"]), 0)
            return json.loads(output.getvalue())

        import hashlib
        digest = hashlib.sha256(b"projection-A").hexdigest()
        before = project()
        self.assertIn(digest, before["digests"])
        with self.unreadable(path), self.assertRaises(OSError):
            project()
        self.assertEqual(project(), before)
        self.replace_ledger(path, "projection-B", "projection-C")
        after = project()
        self.assertNotEqual(after["generation"], before["generation"])
        self.assertNotIn(digest, after["digests"])

    def test_opencode_projection_rechecks_a_replacement_after_storage_discovery(self):
        seat = self.edge("opencode", "project-race-A", "project-race-B")
        path = tidy._ledger_path(seat)
        original = handover.storage_recipients

        def discover(*args, **kwargs):
            scopes = original(*args, **kwargs)
            self.replace_ledger(path, "project-race-B", "project-race-C")
            return scopes

        output = io.StringIO()
        with mock.patch.object(handover, "pane_seat", return_value=seat), \
                mock.patch.object(handover, "storage_recipients", side_effect=discover), \
                contextlib.redirect_stdout(output), self.assertRaises(OSError):
            sweep.main(["recipients", "--recipient-kind", "opencode-turn", "--session", "project-race-B"])
        self.assertEqual(output.getvalue(), "")

    def test_warm_atomic_replacement_at_real_first_write_keeps_original_records(self):
        import codex_queue_delivery
        import codex_queue_dispatch
        for harness in ("claude", "opencode", "codex"):
            old, current, new = (harness + "-write-" + suffix for suffix in ("A", "B", "C"))
            notice, ref, recipient = self.prepared_notice(harness, old, current, old)
            path = tidy._ledger_path(self.edge(harness, old, current))
            record = pending.read(self.tmp_root, old, notice["delivery_id"])
            identity = pending.received_ack_identity(record, old, notice["delivery_id"])
            self.assertEqual(handover.record_recipient(notice, old, harness=harness), current)
            if harness != "codex":
                reserved = steward.peer_message.reserve_pending_herdr(ref, recipient)
                self.assertIsNotNone(reserved)
                transfer = steward.peer_message._read_pending(ref)
                self.replace_ledger(path, current, new)
                self.assertIsNone(steward.peer_message.begin_pending_write(reserved, recipient=recipient))
                self.assertEqual(steward.peer_message._read_pending(ref), transfer)
            else:
                added = []

                def send(*args, **kwargs):
                    if kwargs["allow_add"]:
                        self.replace_ledger(path, current, new)
                        kwargs["before_add"]()
                        added.append(current)
                    return {"status": "queued"}

                with mock.patch.object(codex_queue_dispatch, "resolve_queue_socket",
                                       return_value=self.tmp_root / "fake-queue.sock"), \
                        mock.patch.object(codex_queue_delivery, "send_at_least_once", side_effect=send):
                    result = steward.peer_message.deliver_pending_codex(ref)
                self.assertEqual(result["status"], "unverified")
                self.assertEqual(added, [])
            self.assertEqual(pending.read(self.tmp_root, old, notice["delivery_id"]), record)
            self.assertEqual(pending.received_ack_identity(record, old, notice["delivery_id"]), identity)
            self.assertIsNone(pending.read(self.tmp_root, new, notice["delivery_id"]))

    def test_absent_and_empty_legacy_state_differ_from_failed_stat_and_enumeration(self):
        notice = self.notice()
        self.assertEqual(handover.record_recipient(notice, "A", harness="claude"), "A")
        empty = tidy._ledger_path(tidy.Seat("pane", "empty-legacy"))
        empty.parent.mkdir(parents=True, exist_ok=True)
        empty.write_bytes(b"")
        self.assertEqual(handover.record_recipient(notice, "A", harness="claude"), "A")
        path = tidy._ledger_path(self.edge("claude", "A", "B"))
        directory = path.parent
        before = self.snapshot(path)
        for method, target in (("stat", directory), ("iterdir", directory), ("lstat", path)):
            real = getattr(Path, method)

            def fail(candidate, *args, **kwargs):
                if candidate == target:
                    raise PermissionError("fixture " + method)
                return real(candidate, *args, **kwargs)

            context = PeerReadContext()
            with self.subTest(method=method), mock.patch.object(Path, method, new=fail):
                self.assertIsNone(handover.record_recipient(notice, "A", harness="claude",
                                                            read_context=context))
                self.assertIsNone(self.counts()["no_recipient"])
            self.assertEqual(handover.record_recipient(notice, "A", harness="claude",
                                                       read_context=context), "B")
            self.assertEqual(self.snapshot(path), before)
        legacy = tidy.Seat("pane", "unrelated-legacy")
        tidy._append_ledger_entry(legacy, {"old-field": "no SID or event"})
        self.assertEqual(handover.record_recipient(notice, "A", harness="claude"), "B")

    def test_malformed_relation_observations_retry_without_any_file_change(self):
        notice = self.notice()
        path = tidy._ledger_path(self.edge("claude", "A", "B"))
        before = self.snapshot(path)
        relation = dict(event="succession", source="tidy-clear", harness="claude",
                        sid="B", **{"from": {"harness": "claude", "sid": "A"}})
        bad_rows = [b'{"event":"succession",', b'[]\n', b'\xff\n']
        for field in ("sid", "harness", "from", "source"):
            row = dict(relation)
            del row[field]
            bad_rows.append(json.dumps(row).encode())
        bad_rows.append(json.dumps(dict(relation, source={})).encode())
        real_read = tidy.read_bytes
        for bad in bad_rows:
            context = PeerReadContext()
            handover._LINEAGE_GRAPH_CACHE.clear()
            with self.subTest(bad=bad), mock.patch.object(tidy, "read_bytes",
                    side_effect=lambda candidate: bad if candidate == path else real_read(candidate)):
                self.assertIsNone(handover.record_recipient(notice, "A", harness="claude",
                                                            read_context=context))
            self.assertEqual(handover.record_recipient(notice, "A", harness="claude",
                                                       read_context=context), "B")
            self.assertEqual(self.snapshot(path), before)

    def test_unavailable_registered_explicit_holder_denies_authority_and_sweeps(self):
        import route_authority
        for harness, recipient_harness in (("claude", "claude"), ("codex", "codex"),
                                           ("opencode", "opencode"), ("claude", "codex")):
            tag = harness + recipient_harness
            meta, record, seat = self.handoff(tag, harness, recipient_harness)
            self.edge(recipient_harness, tag + "-Z", tag + "-Y")
            path = tidy._ledger_path(seat)
            before = self.snapshot(path)
            context = PeerReadContext()
            handover._LINEAGE_GRAPH_CACHE.clear()
            with self.subTest(harness=harness, recipient_harness=recipient_harness), self.unreadable(path):
                self.assertIsNone(handover.record_recipient(record, tag + "-A", harness=harness,
                                                            read_context=context))
                self.assertEqual(handover.effective_parent(meta, self.jobs_path, read_context=context), "")
                for sid in (tag + "-A", tag + "-B", tag + "-Z", tag + "-Y"):
                    self.assertFalse(handover.owns(meta, sid, self.jobs_path, read_context=context))
                    self.assertFalse(route_authority.decide_continuation(action="handover",
                        jobs=self.jobs_path, metadata=meta, status="running",
                        facts={"eligible": True, "actor": sid}).allowed)
            self.assertEqual(handover.record_recipient(record, tag + "-A", harness=harness,
                                                       read_context=context), tag + "-Y")
            self.assertEqual(self.snapshot(path), before)
        notice = self.notice()
        path = tidy._ledger_path(self.edge("claude", "A", "B"))
        handover._LINEAGE_GRAPH_CACHE.clear()
        with self.unreadable(path):
            for sid in ("A", "B"):
                self.assertEqual(sweep.sweep_deliver(self.tmp_root, "claude-parent-runtime", sid)[0], [])
        self.assertEqual(pending.read(self.tmp_root, "A", notice["delivery_id"])["state"], "pending")

    def test_unbound_registered_parent_stays_unresolved_during_read_failure(self):
        meta = dict(attempt_id="att-" + "a" * 32, parent_sid="A", parent_harness="claude",
                    dispatch_depth="1", worker_type="frame", route_id="rt-" + "a" * 16,
                    route_hash="sha256:" + "b" * 64, route_node="execute")
        self.jobs_path.write_text("now\trunning\t/w\t/w\ttask\t" +
                                 ",".join(k + "=" + v for k, v in meta.items()) + "\n")
        record = {"attempt_ids": [meta["attempt_id"]]}
        path = tidy._ledger_path(self.edge("claude", "A", "B"))
        context = PeerReadContext()
        with self.unreadable(path):
            self.assertIsNone(handover.record_recipient(record, "A", harness="claude", read_context=context))
            self.assertEqual(handover.effective_parent(meta, self.jobs_path, read_context=context), "")
            for sid in ("A", "B"):
                self.assertFalse(handover.owns(meta, sid, self.jobs_path, read_context=context))
        self.assertEqual(handover.record_recipient(record, "A", harness="claude", read_context=context), "B")

    def test_real_preparation_and_last_native_guard_refuse_unavailable_lineage(self):
        for boundary in ("readiness", "last-guard"):
            old, current = boundary + "-A", boundary + "-B"
            notice = self.notice(sid=old, key=old)
            path = tidy._ledger_path(self.edge("claude", old, current))
            failed = False
            calls = []

            def outage():
                nonlocal failed
                failed = True

            def readiness(*args, **kwargs):
                if boundary == "readiness":
                    outage()
                return SimpleNamespace(state="ready")

            def guard(*args, **kwargs):
                calls.append(True)
                if boundary == "last-guard" and len(calls) == 2:
                    outage()
                return None, {}

            with self.subTest(boundary=boundary), self.unreadable(path, enabled=lambda: failed), \
                    self.transport(current, readiness=readiness) as native, \
                    mock.patch.object(steward, "_claude_idle_prompt_guard", side_effect=guard):
                steward._resume_session_notice_obligations(self.duties(), self.store())
            self.assertTrue(failed)
            native.assert_not_called()
            row = pending.read(self.tmp_root, old, notice["delivery_id"])
            self.assertEqual(row["state"], "sent-ambiguous" if boundary == "last-guard" else "pending")

    def test_deferred_begin_write_and_codex_before_add_refuse_then_recover(self):
        import codex_queue_delivery
        import codex_queue_dispatch
        for harness in ("claude", "opencode", "codex"):
            old, current = harness + "-A", harness + "-B"
            notice, ref, recipient = self.prepared_notice(harness, old, current, old)
            path = tidy._ledger_path(self.edge(harness, old, current))
            record = pending.read(self.tmp_root, old, notice["delivery_id"])
            if harness != "codex":
                reserved = steward.peer_message.reserve_pending_herdr(ref, recipient)
                with self.subTest(harness=harness), self.unreadable(path):
                    self.assertIsNone(steward.peer_message.begin_pending_write(reserved, recipient=recipient))
                self.assertEqual(steward.peer_message.begin_pending_write(reserved, recipient=recipient)["state"],
                                 "unverified")
                self.assertIsNone(steward.peer_message.begin_pending_write(reserved, recipient=recipient))
            else:
                added = []

                def send(*args, **kwargs):
                    if kwargs["allow_add"]:
                        with self.unreadable(path):
                            kwargs["before_add"]()
                        added.append(current)
                    return {"status": "queued"}

                with mock.patch.object(codex_queue_dispatch, "resolve_queue_socket",
                                       return_value=Path("/tmp/fake-queue.sock")), \
                        mock.patch.object(codex_queue_delivery, "send_at_least_once", side_effect=send):
                    result = steward.peer_message.deliver_pending_codex(ref)
                self.assertEqual(result["status"], "unverified")
                self.assertEqual(added, [])
            self.assertEqual(pending.read(self.tmp_root, old, notice["delivery_id"]), record)

    def test_historical_receipt_and_ack_retry_settle_during_current_observer_outage(self):
        for ack_error in (False, True):
            old, current = "ack-A-" + str(ack_error), "ack-B-" + str(ack_error)
            notice = self.notice(sid=old, key=old)
            path = tidy._ledger_path(self.edge("claude", old, current))
            with self.transport(current) as native:
                steward._resume_session_notice_obligations(self.duties(), self.store())
            native.assert_called_once()
            duty = next(d for d in self.duties() if d["intent"]["identity"]["storage_sid"] == old)
            ref = duty["observation"]["transfer_ref"]
            transfer = steward.peer_message._read_pending(ref)
            with self.unreadable(path), mock.patch.object(pending, "ack_received",
                    side_effect=OSError("fixture ACK write") if ack_error else None,
                    wraps=None if ack_error else pending.ack_received):
                steward.peer_message.receive_peer_message(transfer["text"],
                    {"harness": "claude", "session_id": current})
            with self.unreadable(path), mock.patch.object(steward, "cmd_prompt") as send:
                steward._resume_session_notice_obligations(self.duties(), self.store())
            send.assert_not_called()
            self.assertEqual(pending.read(self.tmp_root, old, notice["delivery_id"])["state"], "acked")

    def test_prepared_queued_ambiguous_missing_and_fenced_transfers_are_not_resent_during_outage(self):
        for state in ("pending", "queued", "unverified", "missing", "fenced"):
            old, current = state + "-A", state + "-B"
            if state == "fenced":
                notice = self.notice(sid=old, key=old)
                row = pending.claim(self.tmp_root, old, notice["delivery_id"], claim_owner="old-queue",
                    lease_seconds=1, require_generation_proof=False, jobs=self.jobs_path)
                identity = pending.submission_fence_identity(row, old)
                pending.prepare_submission_fence(self.tmp_root, old, notice["delivery_id"],
                    claim_owner=row["claim_owner"], expected_identity=identity)
            else:
                notice, ref, _recipient = self.prepared_notice("claude", old, current, old)
                transfer = steward.peer_message._read_pending(ref)
                if state in {"queued", "unverified"}:
                    steward.peer_message._save_pending(dict(transfer, state=state))
                elif state == "missing":
                    steward.peer_message._pending_path(ref).unlink()
            path = tidy._ledger_path(self.edge("claude", old, current))
            before = pending.read(self.tmp_root, old, notice["delivery_id"])
            with self.subTest(state=state), self.unreadable(path), self.transport(current) as native:
                steward._resume_session_notice_obligations(self.duties(), self.store())
            native.assert_not_called()
            self.assertEqual(pending.read(self.tmp_root, old, notice["delivery_id"]), before)

    def test_pending_record_failures_invalidate_mixed_counts_and_recover(self):
        notices = [self.notice(sid=sid, key=sid) for sid in ("dead-A", "dead-B")]
        for sid in ("dead-A", "dead-B"):
            tidy._append_ledger_entry(tidy.Seat("pane", sid), dict(event="start", harness="claude", sid=sid))
        paths = [pending.record_path(self.tmp_root, sid, notice["delivery_id"])
                 for sid, notice in zip(("dead-A", "dead-B"), notices)]
        before = [self.snapshot(path) for path in paths]
        self.assertEqual(self.counts()["no_recipient"], 2)
        real_read = Path.read_text
        for bad in (PermissionError("fixture pending read"), "not-json", "[]", "{}",
                    '{"state":[]}', '{"state":"unrecognized"}'):
            def read(path, *args, **kwargs):
                if path == paths[0]:
                    if isinstance(bad, Exception):
                        raise bad
                    return bad
                return real_read(path, *args, **kwargs)

            with self.subTest(bad=str(bad)), mock.patch.object(Path, "read_text", new=read):
                counts = self.counts()
                self.assertIsNone(counts["no_recipient"])
                self.assertEqual(counts["pending"], 1)
            self.assertEqual(self.counts()["no_recipient"], 2)
            self.assertEqual([self.snapshot(path) for path in paths], before)

    def test_legacy_terminal_rows_need_only_the_existing_state_discriminator(self):
        notice = self.notice(sid="dead-A")
        path = pending.record_path(self.tmp_root, "dead-A", notice["delivery_id"])
        before = self.snapshot(path)
        real = Path.read_text
        for state in ("acked", "rejected", "expired"):
            def read(candidate, *args, **kwargs):
                return json.dumps({"state": state}) if candidate == path else real(candidate, *args, **kwargs)

            with self.subTest(state=state), mock.patch.object(Path, "read_text", new=read):
                counts = self.counts()
                self.assertEqual(counts, dict(pending=0, expired=int(state == "expired"),
                                             retained_batch_duties=0, no_recipient=0))
            self.assertEqual(self.snapshot(path), before)

    def test_pending_empty_directory_and_failed_enumerations_are_distinct(self):
        root = self.tmp_root / "pending-delivery"
        root.mkdir(exist_ok=True)
        self.assertEqual(self.counts()["no_recipient"], 0)
        notice = self.notice(sid="dead-A")
        tidy._append_ledger_entry(tidy.Seat("pane", "dead-A"), dict(event="start", harness="claude", sid="dead-A"))
        path = pending.record_path(self.tmp_root, "dead-A", notice["delivery_id"])
        before = self.snapshot(path)
        for method, target in (("stat", root), ("iterdir", root), ("stat", path.parent), ("iterdir", path.parent)):
            real = getattr(Path, method)

            def fail(candidate, *args, **kwargs):
                if candidate == target:
                    raise PermissionError("fixture pending enumeration")
                return real(candidate, *args, **kwargs)

            with self.subTest(method=method, target=str(target)), mock.patch.object(Path, method, new=fail):
                self.assertIsNone(self.counts()["no_recipient"])
            self.assertEqual(self.counts()["no_recipient"], 1)
            self.assertEqual(self.snapshot(path), before)

    def test_one_unavailable_pending_directory_keeps_other_legacy_counts(self):
        paths = []
        for sid in ("dead-A", "dead-B"):
            notice = self.notice(sid=sid, key=sid)
            tidy._append_ledger_entry(tidy.Seat("pane", sid), dict(event="start", harness="claude", sid=sid))
            paths.append(pending.record_path(self.tmp_root, sid, notice["delivery_id"]))
        real = Path.stat

        def fail(path, *args, **kwargs):
            if path == paths[0].parent:
                raise PermissionError("fixture one recipient directory")
            return real(path, *args, **kwargs)

        before = [self.snapshot(path) for path in paths]
        with mock.patch.object(Path, "stat", new=fail):
            counts = self.counts()
            self.assertEqual(counts["pending"], 1)
            self.assertIsNone(counts["no_recipient"])
        self.assertEqual(self.counts()["no_recipient"], 2)
        self.assertEqual([self.snapshot(path) for path in paths], before)

    def test_invalid_duplicate_is_observed_before_deduplication(self):
        notice = self.notice(sid="dead-A")
        tidy._append_ledger_entry(tidy.Seat("pane", "dead-A"), dict(event="start", harness="claude", sid="dead-A"))
        path = pending.record_path(self.tmp_root, "dead-A", notice["delivery_id"])
        duplicate = path.parent.parent / "zz-invalid-copy" / path.name
        duplicate.parent.mkdir()
        duplicate.write_bytes(b"not JSON")
        before = [self.snapshot(p) for p in (path, duplicate)]
        self.assertIsNone(self.counts()["no_recipient"])
        self.assertEqual([self.snapshot(p) for p in (path, duplicate)], before)

    def registered_record(self, tag, target_harness):
        meta, _record, seat = self.handoff(tag, "claude", target_harness)
        parent = "att-" + "b" * 32
        receipt = dict(schema_version=2, state="delivered", parent_attempt_id=parent,
            job_registry=str(self.jobs_path), children=[dict(attempt_id=meta["attempt_id"], status="done",
            readiness="ready", required_action="inspect-done-failure", reason="terminal-failure-or-unclosed",
            harness="claude")])
        record = pending.create(root=self.tmp_root, recipient_kind="claude-parent-runtime",
            recipient_key=tag + "-A", delivery_id="delivery-" + meta["attempt_id"][4:],
            session_generation="", session_generation_supported="0", attempt_ids=[meta["attempt_id"]],
            parent_attempt_id=parent, route_id=meta["route_id"], route_node="execute", receipt=receipt,
            receipt_digest=pending._canonical_receipt_digest(receipt), row_revisions={meta["attempt_id"]: "original"})
        return meta, record, seat

    def test_typed_record_and_storage_scopes_keep_current_harness_pair(self):
        for source, target in (("claude", "codex"), ("codex", "claude")):
            tag = "pair-" + source
            meta, record, seat = self.handoff(tag, source, target)
            from harness_capabilities import parent_completion
            carrier = parent_completion(source)["carrier"]
            body = {"schema_version": 2, "state": "ready", "parent_attempt_id": "att-pair-parent",
                    "job_registry": str(self.jobs_path), "children": []}
            record = pending.create(self.tmp_root, recipient_kind=carrier, recipient_key=tag + "-A",
                delivery_id="delivery-pair-" + source, session_generation="", session_generation_supported="0",
                attempt_ids=record["attempt_ids"], parent_attempt_id="att-pair-parent", route_id=meta["route_id"],
                route_node="execute", receipt=body, receipt_digest=pending._canonical_receipt_digest(body),
                row_revisions={meta["attempt_id"]: "revision"})
            context = PeerReadContext()
            for recipient in (tag + "-Z", tag + "-Y"):
                if recipient.endswith("Y"):
                    self.edge(target, tag + "-Z", recipient)
                for harness in (source, target):
                    with self.subTest(source=source, target=target, recipient=recipient, harness=harness):
                        self.assertEqual(handover.record_recipient_binding(record, tag + "-A",
                            harness=harness, read_context=context), (target, recipient))
                        self.assertEqual(handover.record_for_session(record, None, session=recipient,
                            harness=harness, storage_key=tag + "-A", read_context=context), harness == target)
                        with mock.patch.object(handover, "pane_seat", return_value=seat):
                            scopes = dict(handover.storage_recipients(recipient, harness=harness))
                        self.assertEqual(tag + "-A" in scopes, harness == target)
                with mock.patch.object(handover, "pane_seat", return_value=seat), \
                        mock.patch("dispatch_notice_state.keep_claim", return_value=True):
                    wrong, _count = sweep.sweep_deliver(self.tmp_root, carrier, recipient)
                self.assertEqual(wrong, [])
                self.assertEqual(pending.read(self.tmp_root, tag + "-A", record["delivery_id"])["state"], "pending")
            # Legacy attempt-only form remains usable without a new harness argument.
            self.assertTrue(handover.record_for_session(record, frozenset(record["attempt_ids"])))

    def test_unavailable_shared_graph_never_confirms_readable_local_history(self):
        seat = self.edge("claude", "history-A", "history-B")
        for sid, ts in (("history-A", 1), ("history-B", 3)):
            tidy._append_ledger_entry(seat, dict(event="start", harness="claude", sid=sid, ts=ts))
        other = tidy._ledger_path(tidy.Seat("pane", "hidden-history"))
        other.write_text(json.dumps(dict(event="start", harness="claude", sid="unrelated", ts=1)) + "\n")
        original = other.read_bytes()
        def project():
            with mock.patch.object(tidy, "_lineage_transcript", return_value={"path": None, "status": "missing"}) as transcripts:
                rows = tidy.seat_lineage(seat, harness="claude", sid="history-B")
            self.assertEqual({call.args[1] for call in transcripts.call_args_list}, {"history-A", "history-B"})
            return rows
        self.assertEqual(project()[0]["relation"], "confirmed")
        real_lstat, real_iterdir = Path.lstat, Path.iterdir
        for failure in ("malformed", "read", "stat", "enumeration", "partial-local"):
            with self.subTest(failure=failure), contextlib.ExitStack() as stack:
                handover._LINEAGE_GRAPH_CACHE.clear()
                if failure == "malformed":
                    other.write_bytes(original + b"{bad\n")
                elif failure == "read":
                    stack.enter_context(self.unreadable(other))
                elif failure == "stat":
                    def lstat(path, *a, **kw):
                        if path == other: raise PermissionError("fixture stat")
                        return real_lstat(path, *a, **kw)
                    stack.enter_context(mock.patch.object(Path, "lstat", lstat))
                elif failure == "enumeration":
                    def iterdir(path):
                        if path == other.parent: raise PermissionError("fixture enumerate")
                        return real_iterdir(path)
                    stack.enter_context(mock.patch.object(Path, "iterdir", iterdir))
                else:
                    local = tidy._ledger_path(seat)
                    before = local.read_bytes()
                    local.write_bytes(before + b"{bad\n")
                rows = project()
                self.assertTrue(rows)
                self.assertTrue(all(row["relation"] == "unknown" and row["predecessor"] is None for row in rows))
                self.assertIsNone(tidy._confirmed_predecessor(rows, "claude", "history-B"))
                if failure == "partial-local": local.write_bytes(before)
            other.write_bytes(original)
            self.assertEqual(project()[0]["relation"], "confirmed")

    def test_fleet_uses_coherent_current_harness_sid_and_successors(self):
        live, paths = set(), []
        for harness in ("codex", "opencode"):
            tag = "fleet-" + harness
            meta, record, _seat = self.registered_record(tag, harness)
            self.edge(harness, tag + "-Z", tag + "-Y")
            self.assertEqual(handover.record_recipient(record, tag + "-A", harness="claude"), tag + "-Y")
            self.assertEqual(handover.effective_parent_harness(meta, self.jobs_path), harness)
            paths.append(pending.record_path(self.tmp_root, tag + "-A", record["delivery_id"]))
            live.add((harness, tag + "-Y"))
        before = [self.snapshot(path) for path in paths]
        self.assertEqual(self.counts(live)["no_recipient"], 0)
        self.assertEqual(self.counts({("claude", sid) for _, sid in live})["no_recipient"], 2)
        self.assertEqual(self.counts()["no_recipient"], 2)
        self.assertEqual([self.snapshot(path) for path in paths], before)
        self.ambiguity("codex", "fleet-codex-Y", "fork")
        self.assertIsNone(self.counts(live)["no_recipient"])

    def test_hidden_fork_merge_cycle_history_and_viewer_share_unknown_relation(self):
        import session_history
        for kind in ("fork", "merge", "cycle"):
            with self.subTest(kind=kind):
                a, b, c = ("history-" + kind + suffix for suffix in ("-A", "-B", "-C"))
                seat = self.edge("claude", a, b)
                for sid, ts in ((a, 1), (b, 2)):
                    tidy._append_ledger_entry(seat, dict(event="start", harness="claude", sid=sid, ts=ts))
                origin, target = (a, c) if kind == "fork" else (c, b) if kind == "merge" else (b, a)
                hidden = self.edge("claude", origin, target)
                paths = [tidy._ledger_path(item) for item in (seat, hidden)]
                before = [self.snapshot(path) for path in paths]
                with mock.patch.object(tidy, "_lineage_transcript", return_value={"path": None, "status": "missing"}):
                    rows = tidy.seat_lineage(seat, harness="claude", sid=b)
                self.assertIsNone(tidy._confirmed_predecessor(rows, "claude", b))
                self.assertIsNone(rows[0]["predecessor"])
                self.assertEqual(rows[0]["relation"], "unknown")
                entries, _reason = session_history._entries_for_generation(rows, 1)
                self.assertTrue(all(row.get("_label") != "확정된 이전 대화" for row in entries))
                self.assertEqual([self.snapshot(path) for path in paths], before)

    def test_same_sid_on_different_harnesses_and_mixed_aggregate(self):
        first, record, _ = self.registered_record("same", "codex")
        second = dict(first, attempt_id="att-" + "e" * 32, route_id="rt-" + "e" * 16)
        with self.jobs_path.open("a") as stream:
            stream.write("now\trunning\t/w\t/w\ttask\t" +
                         ",".join(k + "=" + v for k, v in second.items()) + "\n")
        seat = tidy.Seat("pane", "same-other")
        tidy._append_ledger_entry(seat, dict(event="handover", source="retire", harness="opencode",
            sid="same-Z", **{"from": "same-B"}, ts=3, bindings=[handover.binding_of(self.jobs_path, second)]))
        aggregate = dict(record, attempt_ids=[first["attempt_id"], second["attempt_id"]])
        self.assertIsNone(handover.record_recipient(aggregate, "same-A", harness="claude"))
        self.assertEqual(self.counts({("codex", "same-Z")})["no_recipient"], 0)
        self.assertEqual(self.counts({("opencode", "same-Z")})["no_recipient"], 1)
        path = pending.record_path(self.tmp_root, "same-A", record["delivery_id"])
        path.write_text(json.dumps(aggregate))
        before = self.snapshot(path)
        self.assertIsNone(self.counts({("codex", "same-Z"), ("opencode", "same-Z")})["no_recipient"])
        self.assertEqual(self.snapshot(path), before)


class BindingScopeDiscoveryTest(peer._TmpRootMixin, unittest.TestCase):
    """Enumeration keeps scopes; the existing per-binding reader grants authority."""

    def setUp(self):
        super().setUp()
        os.environ["AGENT_HOME"] = str(ROOT)
        handover._LINEAGE_GRAPH_CACHE.clear()

    def seed(self, tag, *, reverse=False, jobs=None, legacy=False, structured=False):
        jobs = jobs or self.jobs_path
        origin, holder = tag + "-A", tag + "-B"
        metas = [dict(attempt_schema_version="2", dispatch_depth="1", worker_type="owner",
            registered_worker="1", attempt_id=f"att-{tag}-{i}", parent_sid=origin,
            parent_harness="claude", route_id=f"rt-{tag}-{i}", route_hash=f"sha256:{tag}-{i}",
            route_node="" if i == 0 else "owner") for i in range(13)]
        jobs.write_text("".join("now\trunning\t/w\t/w\tfixture\t" +
            ",".join(k + "=" + v for k, v in meta.items()) + "\n" for meta in metas))
        bindings = [handover.binding_of(jobs, meta) for meta in metas]
        scopes = [bindings[:12], bindings[12:], bindings[4:7], bindings[:12]]
        if reverse:
            scopes.reverse()
        seats = []
        for i, scope in enumerate(scopes):
            seat = tidy.Seat("pane", f"{tag}-seat-{i}")
            seats.append(seat)
            with tidy.seat_lock(seat.key):
                handover.publish_snapshot_locked(seat, "claude", origin, scope, jobs=jobs)
                row = dict(event="handover", harness="codex", sid=holder,
                    **{"from": {"harness": "claude", "sid": origin} if structured else origin},
                    ts=100 - i if reverse else i + 1, bindings=scope)
                if not legacy:
                    row["source"] = "retire"
                tidy._append_ledger_entry(seat, row)
        return jobs, origin, holder, metas, bindings, seats

    def candidates(self, jobs, holder, harness="codex", context=None):
        return handover._handover_candidates(harness, holder, jobs, read_context=context)

    def test_read_only_discovery_and_bindings_share_observed_bytes_and_parsing(self):
        jobs, origin, holder, metas, bindings, seats = self.seed("one-observation", structured=True)
        paths = {path for seat in seats for path in ledger_copies(
            seat, json.loads(tidy._ledger_path(seat).read_text()))}
        context = PeerReadContext()
        real_read = tidy.read_bytes
        raw = []
        def read(path):
            data = real_read(path)
            if Path(path).suffix == ".jsonl":
                raw.append((str(path), len(data or b"")))
            return data
        with mock.patch.object(tidy, "read_bytes", side_effect=read), \
                mock.patch.object(tidy, "_read_ledger_lines", wraps=tidy._read_ledger_lines) as parses, \
                mock.patch.object(handover, "pane_seat", return_value=seats[0]):
            graph = handover.lineage_graph(read_context=context)
            self.assertEqual(Counter(Path(path) for path, _size in raw), Counter(paths))
            self.assertEqual(Counter(tidy._ledger_path(call.args[0]) for call in parses.call_args_list),
                             Counter(paths))
            observed = list(raw)
            self.assertEqual(len(self.candidates(jobs, holder, context=context)), 13)
            self.assertEqual(len(handover.current_bindings("codex", holder, jobs, read_context=context)), 13)
            self.assertEqual(len(handover._handover_binding_keys_for_session(jobs, holder,
                                                                            read_context=context)), 13)
            self.assertEqual(dict(handover.storage_recipients(holder, harness="codex",
                read_context=context))[origin], frozenset(meta["attempt_id"] for meta in metas))
            for meta in metas:
                record = {"attempt_ids": [meta["attempt_id"]]}
                self.assertEqual(handover.record_recipient_binding(record, origin, harness="claude",
                    jobs=jobs, read_context=context), ("codex", holder))
                self.assertEqual(handover.effective_parent(meta, jobs, read_context=context), holder)
            self.assertEqual(raw, observed)
            self.assertEqual(parses.call_count, len(paths))
            # The next existing observation reads current bytes, even when it
            # reuses both the context and the global parsed graph.
            self.assertIs(handover.lineage_graph(read_context=context), graph)
            self.assertEqual(raw, observed + observed)
            self.assertEqual(parses.call_count, len(paths))

    def test_string_and_structured_explicit_scopes_follow_only_qualified_holder_hops(self):
        for legacy in (False, True):
            for structured in (False, True):
                for reverse in (False, True):
                    tag = f"origins-{legacy}-{structured}-{reverse}"
                    jobs = self.tmp_root / (tag + ".registry")
                    jobs, origin, holder, metas, bindings, seats = self.seed(tag, jobs=jobs,
                        legacy=legacy, structured=structured, reverse=reverse)
                    context = PeerReadContext()
                    for current in (holder, holder + "-C", holder + "-D"):
                        if current != holder:
                            previous = holder if current.endswith("-C") else holder + "-C"
                            tidy._append_ledger_entry(tidy.Seat("pane", current), dict(event="succession",
                                source="tidy-clear", harness="codex", **{"from": previous}, sid=current, ts=200))
                        with self.subTest(legacy=legacy, structured=structured, reverse=reverse, current=current):
                            self.assertTrue(handover._chain_to(handover.handover_rows(seats[0]), current))
                            self.assertEqual(len(handover._handover_binding_keys_for_session(jobs, current)), 13)
                            self.assertEqual(len(self.candidates(jobs, current, context=context)), 13)
                            self.assertEqual(len(handover.current_bindings("codex", current, jobs)), 13)
                            for meta in metas:
                                record = {"attempt_ids": [meta["attempt_id"]]}
                                self.assertEqual(handover.record_recipient_binding(record, origin, harness="claude",
                                    jobs=jobs, read_context=context), ("codex", current))
                                self.assertFalse(handover.record_for_session(record, None, session=current,
                                    harness="claude", storage_key=origin, jobs=jobs, read_context=context))
                            with mock.patch.object(handover, "pane_seat", return_value=seats[0]):
                                self.assertEqual(len(dict(handover.storage_recipients(current,
                                    harness="codex"))[origin]), 13)
                            unbound = dict(metas[0], route_id="unbound")
                            self.assertEqual(handover.effective_parent(unbound, jobs), origin)
                            self.assertEqual(handover.chain_end("claude", origin), (origin, "self"))
                    tidy._append_ledger_entry(tidy.Seat("pane", tag + "-away"), dict(event="handover",
                        source="retire", harness="opencode", sid=tag + "-elsewhere", ts=300,
                        **{"from": {"harness": "codex", "sid": current}}, bindings=[bindings[0]]))
                    self.assertEqual(len(self.candidates(jobs, current, context=context)), 12)
                    self.assertEqual(handover.effective_parent(metas[0], jobs), tag + "-elsewhere")
                    self.assertEqual(handover.effective_parent_harness(metas[0], jobs), "opencode")

    def test_invalid_structured_origins_leave_all_authority_unavailable_and_recover(self):
        for legacy in (False, True):
            jobs, origin, holder, metas, _bindings, seats = self.seed("invalid-" + str(legacy), legacy=legacy)
            originals = {tidy._ledger_path(seat): tidy._ledger_path(seat).read_bytes() for seat in seats}
            for bad in ({"harness": "unsupported", "sid": origin}, {"sid": origin},
                        {"harness": None, "sid": origin}, {"harness": [], "sid": origin},
                        {"harness": "claude", "sid": None}, {"harness": "claude", "sid": []}):
                with self.subTest(legacy=legacy, origin=bad):
                    for path, raw in originals.items():
                        rows = [json.loads(line) for line in raw.splitlines()]
                        for row in rows:
                            if row.get("event") == "handover":
                                row["from"] = bad
                        path.write_text("".join(json.dumps(row) + "\n" for row in rows))
                    with self.assertRaises(handover.RegistryUnreadable):
                        self.candidates(jobs, holder)
                    self.assertEqual(handover.effective_parent(metas[0], jobs), "")
                    with self.assertRaises(handover.RegistryUnreadable):
                        handover.current_bindings("codex", holder, jobs)
            for path, raw in originals.items():
                path.write_bytes(raw)
            self.assertEqual(len(self.candidates(jobs, holder)), 13)

    def test_structured_explicit_scopes_keep_global_refusal_and_current_admission(self):
        seed = self.seed
        for legacy in (False, True):
            def structured_seed(tag, **kwargs):
                return seed(f"structured-{legacy}-" + tag, structured=True, legacy=legacy, **kwargs)
            with self.subTest(legacy=legacy), mock.patch.object(self, "seed", side_effect=structured_seed):
                self.test_global_fork_merge_cycle_and_different_origins_never_use_local_scope_fallback()
                self.test_manual_clear_is_not_an_implicit_edge_and_global_incomplete_is_not_local_authority()
                self.test_candidate_admission_rechecks_explicit_authority_after_discovery()

    def test_disjoint_overlapping_duplicate_scopes_and_owner_keys_ignore_publication_order(self):
        for reverse in (False, True):
            for named in (False, True):
                with self.subTest(reverse=reverse, named=named):
                    tag = f"scopes-{reverse}-{named}"
                    jobs = self.tmp_root / (tag + ".registry") if named else self.jobs_path
                    jobs, origin, holder, metas, bindings, seats = self.seed(tag, reverse=reverse, jobs=jobs)
                    before = jobs.read_bytes()
                    expected = {handover._logical_key(b) for b in bindings}
                    snapshots = handover._all_snapshots()
                    with mock.patch.object(handover, "_all_snapshots", return_value=list(reversed(snapshots))):
                        possible = self.candidates(jobs, holder, context=PeerReadContext())
                    self.assertEqual(len(possible), 13)
                    self.assertEqual({handover._logical_key(b) for b, _ in possible}, expected)
                    self.assertIn((metas[0]["route_id"], metas[0]["route_hash"], ""),
                                  handover._handover_binding_keys_for_session(jobs, holder))
                    self.assertEqual(len(handover.current_bindings("codex", holder, jobs)), 13)
                    with mock.patch.object(handover, "pane_seat", return_value=seats[0]):
                        self.assertEqual(dict(handover.storage_recipients(holder, harness="codex"))[origin],
                                         frozenset(meta["attempt_id"] for meta in metas))
                    self.assertTrue(all(handover.effective_parent(meta, jobs) == holder for meta in metas))
                    self.assertEqual(jobs.read_bytes(), before)

    def test_legacy_scopes_follow_same_harness_hops_and_explicit_away_precedes_tidy(self):
        jobs, origin, holder, metas, bindings, seats = self.seed("legacy-scopes", legacy=True)
        context = PeerReadContext()
        previous = holder
        for index, current in enumerate((holder + "-C", holder + "-D")):
            tidy._append_ledger_entry(tidy.Seat("pane", f"legacy-hop-{index}"), dict(event="succession",
                source="tidy-clear", harness="codex", **{"from": previous}, sid=current, ts=200 + index))
            self.assertEqual(len(self.candidates(jobs, current, context=context)), 13)
            previous = current
        away = dict(event="handover", source="retire", harness="opencode", sid="away-holder",
                    **{"from": current}, ts=300, bindings=[bindings[0]])
        tidy._append_ledger_entry(tidy.Seat("pane", "legacy-away"), away)
        self.assertEqual(handover.effective_parent(metas[0], jobs, read_context=context), "away-holder")
        self.assertEqual(handover.effective_parent_harness(metas[0], jobs), "opencode")
        self.assertEqual(len(self.candidates(jobs, current, context=context)), 12)
        self.assertEqual(self.candidates(jobs, holder), [])
        # A source-less explicit row never becomes an unbound edge for another job.
        unbound = dict(metas[0], attempt_id="unbound", route_id="unbound", route_node="owner")
        self.assertEqual(handover.effective_parent(unbound, jobs), origin)
        with mock.patch.object(handover, "pane_seat", return_value=seats[0]):
            self.assertEqual(dict(handover.storage_recipients(current, harness="codex"))[origin],
                             frozenset(meta["attempt_id"] for meta in metas[1:]))

    def test_global_fork_merge_cycle_and_different_origins_never_use_local_scope_fallback(self):
        for kind in ("fork", "merge", "cycle", "explicit-merge"):
            with self.subTest(kind=kind):
                jobs, origin, holder, _metas, bindings, seats = self.seed(kind)
                target = holder + "-C"
                tidy._append_ledger_entry(seats[0], dict(event="succession", source="tidy-clear",
                    harness="codex", **{"from": holder}, sid=target, ts=200))
                left, right = ((holder, "fork-D") if kind == "fork" else
                               ("merge-X", target) if kind == "merge" else
                               (target, holder) if kind == "cycle" else ("explicit-X", target))
                row = dict(event="handover" if kind == "explicit-merge" else "succession",
                    source="retire" if kind == "explicit-merge" else "tidy-clear", harness="codex",
                    **{"from": left}, sid=right, ts=300,
                    bindings=[bindings[0]] if kind == "explicit-merge" else [])
                tidy._append_ledger_entry(tidy.Seat("pane", kind + "-hidden"), row)
                if kind == "explicit-merge":
                    # Seat provenance is ambiguous, but each exact job still has
                    # one current holder. Discovery must use that job answer.
                    self.assertEqual(handover.resolve_recipient("seat-successor",
                        session=holder, harness="codex").state, "unreadable")
                    self.assertEqual(len(handover._handover_binding_keys_for_session(jobs, target)), 13)
                    self.assertEqual(len(self.candidates(jobs, target)), 13)
                    for meta in _metas:
                        self.assertEqual(handover.effective_parent(meta, jobs), target)
                        self.assertFalse(handover.owns(meta, origin, jobs))
                        self.assertTrue(handover.owns(meta, target, jobs))
                else:
                    self.assertEqual(handover._handover_binding_keys_for_session(jobs, target), ())
                    self.assertEqual(self.candidates(jobs, target), [])

    def test_manual_clear_is_not_an_implicit_edge_and_global_incomplete_is_not_local_authority(self):
        jobs, _origin, holder, metas, _bindings, seats = self.seed("unavailable")
        local = handover.handover_rows(seats[0])
        hidden = tidy.Seat("pane", "unavailable-hidden")
        tidy._append_ledger_entry(hidden, dict(event="start", harness="codex", sid="manual-C", ts=200))
        self.assertEqual(self.candidates(jobs, "manual-C"), [])
        path = tidy._ledger_path(hidden)
        original = path.read_bytes()
        for failure in ("partial", "corrupt", "unreadable"):
            with self.subTest(failure=failure), contextlib.ExitStack() as stack:
                if failure in {"partial", "corrupt"}:
                    path.write_bytes(original + (b'{"event":' if failure == "partial" else b'{bad}\n'))
                else:
                    real_open = os.open
                    def open_file(candidate, *args, **kwargs):
                        if Path(candidate) == path:
                            raise PermissionError("fixture unavailable global ledger")
                        return real_open(candidate, *args, **kwargs)
                    stack.enter_context(mock.patch.object(tidy.os, "open", side_effect=open_file))
                handover._LINEAGE_GRAPH_CACHE.clear()
                self.assertTrue(local)
                with self.assertRaises(handover.RegistryUnreadable):
                    self.candidates(jobs, holder)
                self.assertEqual(handover.effective_parent(metas[0], jobs), "")
            path.write_bytes(original)
        self.assertEqual(len(self.candidates(jobs, holder)), 13)

    def test_candidate_admission_rechecks_explicit_authority_after_discovery(self):
        import route_authority
        jobs, _origin, holder, metas, bindings, _seats = self.seed("use-time")
        decide = route_authority.decide_continuation
        observed = []
        def move_then_admit(**kwargs):
            if not observed:
                tidy._append_ledger_entry(tidy.Seat("pane", "use-time-away"),
                    dict(event="handover", source="retire", harness="claude", sid="latest-holder",
                         **{"from": holder}, ts=400, bindings=[bindings[0]]))
            observed.append(kwargs["metadata"]["attempt_id"])
            return decide(**kwargs)
        with mock.patch.object(route_authority, "decide_continuation", side_effect=move_then_admit):
            possible = self.candidates(jobs, holder)
        self.assertEqual(len(observed), 13)
        self.assertEqual(len(possible), 12)
        self.assertNotIn(metas[0]["attempt_id"], {b["attempt"] for b, _ in possible})
        self.assertEqual(handover.effective_parent(metas[0], jobs), "latest-holder")


if __name__ == "__main__":
    unittest.main(verbosity=2)
