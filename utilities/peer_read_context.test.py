#!/usr/bin/env python3
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


if __name__ == "__main__":
    unittest.main()
