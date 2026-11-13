#!/usr/bin/env python3
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import dispatch_completion_join as JOIN  # noqa: E402
import dispatch_registry_cache as CACHE  # noqa: E402
from peer_read_context import PeerReadContext  # noqa: E402


def row(attempt, status="open", schema="2", slug="worker"):
    return f"job\t{status}\towner\t2026-10-17T00:00:00Z\t{slug}\tattempt_id={attempt},attempt_schema_version={schema}"


class PeerReadContextTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.jobs = Path(self.tmp.name) / "jobs.log"
        CACHE.clear_cache()

    def test_attempt_rows_are_indexed_once_and_keep_latest_exact_row(self):
        self.jobs.write_text("\n".join((row("att-a", "queued"), row("att-b"),
                                          row("att-a", "running", slug="latest"))) + "\n")
        CACHE.reset_metrics()
        context = PeerReadContext()
        first = JOIN.current_attempt_row(self.jobs, "att-a", read_context=context)
        second = JOIN.current_attempt_row(self.jobs, "att-b", read_context=context)
        again = JOIN.current_attempt_row(self.jobs, "att-a", read_context=context)
        self.assertEqual((first.status, first.slug, first.order), ("running", "latest", 2))
        self.assertEqual(second.attempt_id, "att-b")
        self.assertEqual(again.raw, first.raw)
        metrics = CACHE.metrics_snapshot()
        self.assertEqual(metrics["content_reads"], 1)
        self.assertEqual(metrics["full_metadata_parses"], 1)

    def test_unrelated_invalid_schema_is_ignored_and_exact_invalid_schema_fails(self):
        self.jobs.write_text(row("att-good") + "\n" + row("att-bad", schema="1") + "\n")
        context = PeerReadContext()
        self.assertEqual(JOIN.current_attempt_row(
            self.jobs, "att-good", read_context=context).attempt_id, "att-good")
        with self.assertRaisesRegex(JOIN.JoinContractError, "attempt-row-schema-invalid"):
            JOIN.current_attempt_row(self.jobs, "att-bad", read_context=context)

    def test_new_context_observes_appends_without_refreshing_an_existing_scan(self):
        self.jobs.write_text(row("att-old") + "\n")
        first_scan = PeerReadContext()
        self.assertIsNone(first_scan.current_attempt_row(self.jobs, "att-new"))
        with self.jobs.open("a", encoding="utf-8") as handle:
            handle.write(row("att-new") + "\n")
        self.assertIsNone(first_scan.current_attempt_row(self.jobs, "att-new"))
        self.assertEqual(PeerReadContext().current_attempt_row(
            self.jobs, "att-new").attempt_id, "att-new")

    def test_equal_attempt_names_in_distinct_jobs_remain_separate(self):
        other = Path(self.tmp.name) / "other-jobs.log"
        self.jobs.write_text(row("att-same", "running", slug="first") + "\n")
        other.write_text(row("att-same", "done", slug="second") + "\n")
        context = PeerReadContext()
        first = context.current_attempt_row(self.jobs, "att-same")
        second = context.current_attempt_row(other, "att-same")
        self.assertEqual((first.status, first.slug), ("running", "first"))
        self.assertEqual((second.status, second.slug), ("done", "second"))

    def test_handover_reads_are_memoized_only_in_this_context(self):
        context = PeerReadContext()
        seat = type("Seat", (), {"kind": "pane", "key": "pane-a"})()
        loader = mock.Mock(return_value=[{"from": "sid-a", "sid": "sid-b"}])
        self.assertEqual(context.handover_rows(seat, loader), context.handover_rows(seat, loader))
        loader.assert_called_once_with()
        other_scan_loader = mock.Mock(return_value=[])
        self.assertEqual(PeerReadContext().handover_rows(seat, other_scan_loader), ())
        other_scan_loader.assert_called_once_with()

    def test_recipient_census_is_memoized_per_endpoint_and_per_scan(self):
        context = PeerReadContext()
        agents = [{"agent": "codex", "agent_session": {"value": "sid-a"}}]
        loader = mock.Mock(return_value=agents)
        self.assertIs(context.recipient_census("server-a", loader), agents)
        self.assertIs(context.recipient_census("server-a", loader), agents)
        loader.assert_called_once_with()

        other_endpoint = mock.Mock(return_value=[])
        self.assertEqual(context.recipient_census("server-b", other_endpoint), [])
        other_endpoint.assert_called_once_with()
        next_scan = mock.Mock(return_value=agents)
        self.assertIs(PeerReadContext().recipient_census("server-a", next_scan), agents)
        next_scan.assert_called_once_with()

    def test_transfer_root_candidates_are_resolved_once_per_scan(self):
        context = PeerReadContext()
        roots = [Path(self.tmp.name) / "current", Path(self.tmp.name) / "legacy"]
        loader = mock.Mock(return_value=(roots, "fixture"))

        first = context.transfer_roots(loader)
        second = context.transfer_roots(loader)

        self.assertEqual(first, tuple(roots))
        self.assertIs(first, second)
        loader.assert_called_once_with()

        next_scan = mock.Mock(return_value=(roots, "fixture"))
        self.assertEqual(PeerReadContext().transfer_roots(next_scan), tuple(roots))
        next_scan.assert_called_once_with()

    def test_peer_notice_scan_is_shared_only_within_one_pass(self):
        context = PeerReadContext()
        notices = [{"transfer_ref": "a" * 32, "delivery": {"status": "received"}}]
        loader = mock.Mock(return_value=notices)
        self.assertIs(context.peer_notice_records(loader), context.peer_notice_records(loader))
        loader.assert_called_once_with()

        next_scan = mock.Mock(return_value=[])
        self.assertEqual(PeerReadContext().peer_notice_records(next_scan), ())
        next_scan.assert_called_once_with()

    def test_lineage_graph_cache_is_generation_keyed_and_pass_local(self):
        context = PeerReadContext()
        first = mock.Mock(return_value="graph-a")
        self.assertEqual(context.lineage_graph(("a",), first), "graph-a")
        self.assertEqual(context.lineage_graph(("a",), first), "graph-a")
        first.assert_called_once_with()
        second = mock.Mock(return_value="graph-b")
        self.assertEqual(context.lineage_graph(("b",), second), "graph-b")
        second.assert_called_once_with()
        fresh = mock.Mock(return_value="next-pass")
        self.assertEqual(PeerReadContext().lineage_graph(("b",), fresh), "next-pass")
        fresh.assert_called_once_with()

    def test_lineage_directory_is_observed_once_inside_a_pinned_loop(self):
        # 2026-10-30: one storage_recipients pass resolved 1,681 recipients and
        # re-stated ~550 ledgers for each, outliving the 10s prompt hook.
        context = PeerReadContext()
        observe = mock.Mock(return_value=("sessions", (), "gen-a"))
        context.lineage_signature(observe)
        context.lineage_signature(observe)
        self.assertEqual(observe.call_count, 2)  # unpinned reads still see writers
        with context.pinned_lineage():
            for _ in range(3):
                self.assertEqual(context.lineage_signature(observe), ("sessions", (), "gen-a"))
            with context.pinned_lineage():
                context.lineage_signature(observe)
            self.assertEqual(observe.call_count, 3)
            context.invalidate_lineage_graph()
            context.lineage_signature(observe)
            self.assertEqual(observe.call_count, 4)
        context.lineage_signature(observe)
        self.assertEqual(observe.call_count, 5)

    def test_history_summary_is_graph_bound_and_cleared_by_replacement_or_failure(self):
        context = PeerReadContext()
        graph_a, graph_b = object(), object()
        first = mock.Mock(return_value={"ledger-a": "summary-a"})
        context.lineage_graph(("generation-a",), lambda: graph_a)
        self.assertIs(context.lineage_summary(graph_a, first), context.lineage_summary(graph_a, first))
        first.assert_called_once_with()
        context.lineage_graph(("generation-b",), lambda: graph_b)
        second = mock.Mock(return_value={"ledger-b": "summary-b"})
        self.assertEqual(context.lineage_summary(graph_b, second), {"ledger-b": "summary-b"})
        context.invalidate_lineage_graph()
        context.lineage_summary(graph_b, second)
        self.assertEqual(second.call_count, 2)
        context.lineage_graph(("generation-b",), lambda: graph_b)
        context.lineage_summary(graph_b, second)
        self.assertEqual(second.call_count, 3)

    def test_the_same_signature_object_is_not_hashed_again_and_nothing_stale_is_served(self):
        class Counting(tuple):
            hashes = 0

            def __hash__(self):
                type(self).hashes += 1
                return super().__hash__()

        signature = Counting(("ledgers", ("a", "b")))
        context = PeerReadContext()
        loads = mock.Mock(return_value="graph-1")
        self.assertEqual(context.lineage_graph(signature, loads), "graph-1")
        hashed = Counting.hashes
        for _ in range(50):
            self.assertEqual(context.lineage_graph(signature, loads), "graph-1")
        self.assertEqual(Counting.hashes, hashed)            # the sweep's later lookups cost no hashing
        loads.assert_called_once_with()
        # An equal signature that is another object finds the same graph; a different one does not.
        self.assertEqual(context.lineage_graph(tuple(signature), loads), "graph-1")
        other = mock.Mock(return_value="graph-2")
        self.assertEqual(context.lineage_graph(("ledgers", ("a", "c")), other), "graph-2")
        self.assertEqual(context.lineage_graph(signature, mock.Mock(return_value="graph-3")), "graph-3")
        # Invalidated: the same object no longer answers from memory.
        context.invalidate_lineage_graph()
        rebuilt = mock.Mock(return_value="graph-4")
        self.assertEqual(context.lineage_graph(signature, rebuilt), "graph-4")
        rebuilt.assert_called_once_with()

    def test_job_holders_answer_as_resolve_recipient_does_and_are_kept_only_per_record_and_graph(self):
        import dispatch_seat_handover as handover
        owner = ("job\tdone\t/repo\t/wt\towner\tworker_type=owner,dispatch_depth=1,attempt_id=att-o,"
                 "parent_sid=sid-parent,parent_harness=claude,route_id=rt-1,route_hash=h1,"
                 "attempt_schema_version=2")
        plain = "job\tdone\t/repo\t/wt\tchild\tworker_type=owner,attempt_id=att-p,parent_sid=sid-parent"
        self.jobs.write_text(owner + "\n" + plain + "\n")
        records = CACHE.registry_view(self.jobs).records
        graph = object.__new__(handover.LineageGraph)
        for name, value in (("edges", ()), ("unresolved", frozenset()), ("ledger_keys", {}),
                            ("ledger_rows", {}), ("binding_rows", {}), ("generation", ()),
                            ("seat_edges", ()), ("seat_unresolved", frozenset()),
                            ("binding_conflicts", frozenset())):
            object.__setattr__(graph, name, value)
        context = PeerReadContext()
        graphs = [graph]
        with mock.patch.object(handover, "_pass_lineage_graph", lambda read_context=None: graphs[0]), \
                mock.patch.dict(handover._HOLDER_MEMO, {"graph": None, "answers": {}}):
            before = handover.holder_metrics()
            first = handover.job_holders(records, self.jobs, read_context=context)
            expected = [handover.resolve_recipient("job-holder", metadata=record.handover_metadata(),
                                                   jobs=self.jobs, read_context=context)
                        for record in records]
            self.assertEqual(first, expected)
            again = handover.job_holders(records, self.jobs, read_context=context)
            self.assertEqual(again, expected)
            counted = handover.holder_metrics()
            self.assertEqual(counted["holder_resolves"] - before["holder_resolves"], 2)   # the first call only
            self.assertEqual(counted["holder_memo_hits"] - before["holder_memo_hits"], 2)  # the second call
            # another record object (a rewritten registry) is asked again, the unchanged one is not
            CACHE.clear_cache()
            rewritten = CACHE.registry_view(self.jobs).records
            handover.job_holders(rewritten[:1], self.jobs, read_context=context)
            self.assertEqual(handover.holder_metrics()["holder_resolves"] - counted["holder_resolves"], 1)
            # another lineage graph object: nothing carries over
            counted = handover.holder_metrics()
            graphs[0] = object.__new__(handover.LineageGraph)
            for name in ("edges", "unresolved", "ledger_keys", "ledger_rows", "binding_rows", "generation",
                         "seat_edges", "seat_unresolved", "binding_conflicts"):
                object.__setattr__(graphs[0], name, getattr(graph, name))
            handover.job_holders(rewritten, self.jobs, read_context=context)
            self.assertEqual(handover.holder_metrics()["holder_resolves"] - counted["holder_resolves"], 2)
            # an unreadable lineage is answered like resolve_recipient does and is never kept
            graphs[0] = None
            def unreadable(read_context=None):
                raise OSError("lineage-unreadable")
            counted = handover.holder_metrics()
            with mock.patch.object(handover, "_pass_lineage_graph", unreadable):
                answers = handover.job_holders(rewritten, self.jobs, read_context=context)
            self.assertTrue(all(answer.state == "unreadable" or answer.reason == "" for answer in answers))
            self.assertEqual(handover.holder_metrics(), counted)


if __name__ == "__main__":
    unittest.main()
