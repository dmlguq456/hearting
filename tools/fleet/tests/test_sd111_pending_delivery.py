#!/usr/bin/env python3
"""SD-111 P6 -- fleet `pending`/`expired` visibility. Read-only observer:
these tests never call anything that transitions a pending-delivery record or
a registry row. Hermetic (temp trees only), stdlib unittest.

Runnable directly or via `python3 -m unittest discover -s tools/fleet/tests
-t . -p 'test_sd111*'`.
"""
import json
import os
import sys
import tempfile
import unittest
from contextlib import nullcontext
from types import SimpleNamespace
from pathlib import Path
from unittest import mock

_TOOLS_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)
_UTILITIES_DIR = os.path.join(os.path.dirname(_TOOLS_DIR), "utilities")
if _UTILITIES_DIR not in sys.path:
    sys.path.insert(0, _UTILITIES_DIR)

from fleet import collectors  # noqa: E402
from fleet.collectors import dispatch, procscan  # noqa: E402
import dispatch_pending_delivery as pending_delivery  # noqa: E402


def _record(**overrides):
    receipt = {
        "schema_version": 2,
        "state": "delivered",
        "parent_attempt_id": "att-0000000000000000000000000000aaaa",
        "job_registry": "/tmp/sd111p6/jobs.log",
        "children": [
            {
                "attempt_id": "att-0000000000000000000000000000bbbb",
                "status": "done",
                "readiness": "ready",
                "reason": "terminal-failure-or-unclosed",
                "required_action": "inspect-done-failure",
                "harness": "claude",
                "delivery_classification": "attention",
            }
        ],
        "delivery_classification": "attention",
    }
    base = dict(
        root=None,
        recipient_kind="claude-parent-runtime",
        recipient_key="sess-p6",
        delivery_id="delivery-" + "a" * 32,
        session_generation="",
        session_generation_supported="0",
        attempt_ids=["att-0000000000000000000000000000bbbb"],
        parent_attempt_id="att-0000000000000000000000000000aaaa",
        route_id="rt-example",
        route_node="execute",
        receipt=receipt,
        receipt_digest=pending_delivery._canonical_receipt_digest(receipt),
        row_revisions={"att-0000000000000000000000000000bbbb": "deadbeef"},
    )
    base.update(overrides)
    if base["root"] is not None and "receipt" not in overrides:
        base["receipt"] = {**receipt, "job_registry": str(Path(base["root"]) / "jobs.log")}
        base["receipt_digest"] = pending_delivery._canonical_receipt_digest(base["receipt"])
    return base


class PendingDeliveryCountsTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="sd111-p6-")
        self.state_root = Path(self._tmp.name) / "state"
        self.state_root.mkdir(parents=True, exist_ok=True)
        self.jobs_path = self.state_root / "jobs.log"
        self.jobs_path.write_text("", encoding="utf-8")
        # The original four-case probe reuses this fixture too. Keep its
        # lineage within the tree covered by the read-only bytes/mtime check.
        state_env = mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(self.state_root / "history")})
        state_env.start()
        self.addCleanup(state_env.stop)

    def tearDown(self):
        self._tmp.cleanup()

    def _write_jobs_row(self, meta: dict[str, str]) -> None:
        pipe = ",".join(f"{k}={v}" for k, v in meta.items())
        line = f"2026-08-28T00:00:00Z\tdone\trepo\t-\tslug\t{pipe}\n"
        with open(self.jobs_path, "a", encoding="utf-8") as handle:
            handle.write(line)

    def test_no_records_no_intent_rows_returns_zero(self):
        counts = dispatch._pending_delivery_counts([str(self.jobs_path)])
        self.assertEqual(counts, {"pending": 0, "expired": 0, "retained_batch_duties": 0,
                                  "no_recipient": None})

    def test_materialized_pending_record_counted_once(self):
        pending_delivery.create(**_record(root=self.state_root))
        counts = dispatch._pending_delivery_counts([str(self.jobs_path)])
        self.assertEqual(counts, {"pending": 1, "expired": 0, "retained_batch_duties": 0,
                                  "no_recipient": None})

    def test_notice_remains_visible_without_any_parent_session(self):
        from fleet import render
        record = pending_delivery.create(**_record(root=self.state_root))
        before = pending_delivery.read(self.state_root, "sess-p6", record["delivery_id"])
        counts = dispatch._pending_delivery_counts([str(self.jobs_path)])
        for process_view in (False, True):
            with mock.patch.object(render, "_PROCESS_VIEW", process_view):
                lines = render._build_lines([], [], "all", False, 0, term_width=80,
                                            pending_delivery=counts)
                text = "\n".join("".join(segment[0] for segment in line) for line in lines if line)
                self.assertIn("delivery pending: 1", text)
                self.assertIn("awaiting recipient", text)
        self.assertEqual(pending_delivery.read(self.state_root, "sess-p6", record["delivery_id"]), before)
        pending_delivery.claim(self.state_root, "sess-p6", record["delivery_id"],
                               claim_owner="test", lease_seconds=30, require_generation_proof=False)
        pending_delivery.ack(self.state_root, "sess-p6", record["delivery_id"], acked_by="prompt")
        lines = render._build_lines([], [], "all", False, 0, term_width=80,
            pending_delivery=dispatch._pending_delivery_counts([str(self.jobs_path)]))
        self.assertNotIn("delivery pending", str(lines))

    def test_expired_record_counted_separately_and_never_deleted(self):
        record = pending_delivery.create(**_record(root=self.state_root))
        pending_delivery.expire_if_due(
            self.state_root, "sess-p6", record["delivery_id"],
            actor="dispatch-reconcile", reason="recipient-session-gone",
            liveness="known",
        )
        counts = dispatch._pending_delivery_counts([str(self.jobs_path)])
        self.assertEqual(counts, {"pending": 0, "expired": 1, "retained_batch_duties": 0,
                                  "no_recipient": None})
        # A-9 visibility clause: the file still exists on disk.
        record_path = pending_delivery.record_path(
            self.state_root, "sess-p6", record["delivery_id"]
        )
        self.assertTrue(record_path.is_file())

    def test_intent_stamped_row_without_a_materialized_record_is_counted(self):
        # The crash window between the terminal commit and trigger 1/2: the
        # row carries delivery_intent but no pending-delivery/ record file
        # exists yet anywhere on disk. Omitting this term would make the
        # completion silently invisible (plan §1.6-1, §9 R-9).
        self._write_jobs_row({
            "attempt_id": "att-intent-only",
            "delivery_intent": "1",
            "parent_sid": "sess-p6",
            "delivery_id": "delivery-" + "b" * 32,
        })
        counts = dispatch._pending_delivery_counts([str(self.jobs_path)])
        self.assertEqual(counts, {"pending": 1, "expired": 0, "retained_batch_duties": 0,
                                  "no_recipient": None})

    def test_intent_row_and_its_own_materialized_record_counted_once_not_twice(self):
        record = pending_delivery.create(**_record(root=self.state_root))
        self._write_jobs_row({
            "attempt_id": "att-0000000000000000000000000000bbbb",
            "delivery_intent": "1",
            "parent_sid": "sess-p6",
            "delivery_id": record["delivery_id"],
        })
        counts = dispatch._pending_delivery_counts([str(self.jobs_path)])
        self.assertEqual(counts, {"pending": 1, "expired": 0, "retained_batch_duties": 0,
                                  "no_recipient": None})

    def test_enumeration_failure_fails_open_missing_directory(self):
        missing_jobs = self.state_root / "does-not-exist" / "jobs.log"
        counts = dispatch._pending_delivery_counts([str(missing_jobs)])
        self.assertEqual(counts, {"pending": 0, "expired": 0, "retained_batch_duties": 0,
                                  "no_recipient": None})

    def test_corrupt_record_file_skipped_not_raised(self):
        pending_root = self.state_root / "pending-delivery" / ("f" * 64)
        pending_root.mkdir(parents=True, exist_ok=True)
        (pending_root / "delivery-broken.json").write_text("not json", encoding="utf-8")
        counts = dispatch._pending_delivery_counts([str(self.jobs_path)])
        self.assertEqual(counts, {"pending": 0, "expired": 0, "retained_batch_duties": 0,
                                  "no_recipient": None})

    def test_retained_batch_duties_counted_while_complete_ones_are_not(self):
        obligations = self.state_root / "supervisor-state" / "obligations"
        obligations.mkdir(parents=True, exist_ok=True)
        for duty_id, state in (("batch-" + "a" * 32, "observing"),
                               ("batch-" + "b" * 32, "delivery-pending"),
                               ("batch-" + "c" * 32, "unknown"),
                               ("batch-" + "d" * 32, "complete")):
            (obligations / f"{duty_id}.json").write_text(
                json.dumps({"schema_version": 1, "id": duty_id, "state": state}),
                encoding="utf-8")
        (obligations / "batch-broken.json").write_text("not json", encoding="utf-8")
        counts = dispatch._pending_delivery_counts([str(self.jobs_path)])
        self.assertEqual(counts, {"pending": 0, "expired": 0, "retained_batch_duties": 3,
                                  "no_recipient": None})

    def test_collect_stashes_counts_on_the_module_like_last_malformed(self):        # A-9/A-21 visibility clause: `collect()` (fleet's one read-only
        # entry point, F-27's control module is the only fleet surface with
        # write authority) stashes the pending-delivery counts on itself the
        # same additive way it already stashes last_malformed/
        # last_route_nodes -- no return-signature change, never a gate.
        pending_delivery.create(**_record(root=self.state_root))
        try:
            with mock.patch.object(dispatch, "_scan_processes", return_value=[]), \
                 mock.patch.object(dispatch, "_live_attempt_ids", return_value=set()), \
                 mock.patch.object(dispatch, "process_table_scan_scope", side_effect=nullcontext):
                dispatch.collect(jobs_path=str(self.jobs_path))
            self.assertEqual(
                dispatch.collect.last_pending_delivery,
                {"pending": 1, "expired": 0, "retained_batch_duties": 0,
                 "no_recipient": None}
            )
        finally:
            dispatch.collect.last_pending_delivery = None

    def test_collect_passes_private_complete_census_to_intent_accounting(self):
        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(self.state_root / "history")}):
            jobs, _, _, holder = self._intent_fixture("collect-intent")
            for live, expected in ((set(), 1), ({holder}, 0)):
                observation = collectors._session_census_observation(
                    procscan._CensusRows([SimpleNamespace(harness=h, session_id=s)
                                         for h, s in live], complete=True), {"complete": True})
                with mock.patch.object(dispatch, "_scan_processes", return_value=[]), \
                     mock.patch.object(dispatch, "_live_attempt_ids", return_value=set()), \
                     mock.patch.object(dispatch, "process_table_scan_scope", side_effect=nullcontext), \
                     mock.patch.object(dispatch, "_scan_jobs_log", return_value=([], 0)):
                    dispatch.collect(jobs_path=str(jobs), session_observation=observation)
                self.assertEqual(dispatch.collect.last_pending_delivery["pending"], 1)
                self.assertEqual(dispatch.collect.last_pending_delivery["no_recipient"], expected)
            dispatch.collect.last_pending_delivery = None

    def _count_with_graph(self, storage_sid, recipient_sid, live=(), duplicate_paths=False,
                          session_observation=None, graph_sids=None,
                          recipient_by_storage=None):
        record = pending_delivery.create(**_record(root=self.state_root,
                                                    recipient_key=storage_sid))
        path = pending_delivery.record_path(self.state_root, storage_sid, record["delivery_id"])
        before = (path.read_bytes(), path.stat().st_mtime_ns)
        graph = SimpleNamespace(ledger_keys={
            ("claude", sid): () for sid in (graph_sids or (storage_sid,))
        })
        paths = [str(self.jobs_path), str(self.jobs_path)] if duplicate_paths else [str(self.jobs_path)]
        if session_observation is None:
            session_observation = {
                "complete": True,
                "herdr_complete": True,
                "process_complete": True,
                "native_sid_complete": True,
                "live": set(live),
            }
        def resolve_recipient(_record, key, *, harness, jobs=None, read_context=None):
            sid = (recipient_by_storage or {}).get(key, recipient_sid)
            return (harness, sid) if sid is not None else None

        with mock.patch("dispatch_seat_handover.lineage_graph", return_value=graph), \
                mock.patch("dispatch_seat_handover.record_recipient_binding",
                           side_effect=resolve_recipient):
            counts = dispatch._pending_delivery_counts(
                paths, session_observation=session_observation)
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)
        return counts

    def _count_with_complete_graph(self, graph_sids, recipient_sid="dead-C"):
        graph = SimpleNamespace(ledger_keys={
            ("claude", sid): () for sid in graph_sids
        })
        observation = {
            "complete": True,
            "herdr_complete": True,
            "process_complete": True,
            "native_sid_complete": True,
            "live": set(),
        }
        with mock.patch("dispatch_seat_handover.lineage_graph", return_value=graph), \
                mock.patch("dispatch_seat_handover.record_recipient_binding",
                           return_value=("claude", recipient_sid) if recipient_sid is not None else None):
            return dispatch._pending_delivery_counts(
                [str(self.jobs_path)], session_observation=observation)

    def _new_pending_file(self, storage_sid, delivery_suffix="a"):
        record = pending_delivery.create(**_record(
            root=self.state_root,
            recipient_key=storage_sid,
            delivery_id="delivery-" + delivery_suffix * 32,
        ))
        return record, pending_delivery.record_path(
            self.state_root, storage_sid, record["delivery_id"]
        )

    def test_dead_storage_and_confirmed_dead_chain_end_count_once(self):
        counts = self._count_with_graph("dead-A", "dead-C", duplicate_paths=True)
        self.assertEqual(counts["no_recipient"], 1)
        self.assertEqual(counts["pending"], 1)

    def test_directory_digest_mismatch_keeps_no_recipient_unknown(self):
        record, path = self._new_pending_file("dead-A")
        row = json.loads(path.read_text(encoding="utf-8"))
        row["recipient_digest"] = pending_delivery.recipient_digest("dead-C")
        path.write_text(json.dumps(row), encoding="utf-8")
        before = (path.read_bytes(), path.stat().st_mtime_ns)

        counts = self._count_with_complete_graph(("dead-A", "dead-C"))

        self.assertEqual(counts["pending"], 1)
        self.assertIsNone(counts["no_recipient"])
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)

    def test_filename_delivery_id_mismatch_keeps_no_recipient_unknown(self):
        _record_value, path = self._new_pending_file("dead-A")
        row = json.loads(path.read_text(encoding="utf-8"))
        row["delivery_id"] = "delivery-" + "b" * 32
        path.write_text(json.dumps(row), encoding="utf-8")
        before = (path.read_bytes(), path.stat().st_mtime_ns)

        counts = self._count_with_complete_graph(("dead-A",))

        self.assertEqual(counts["pending"], 1)
        self.assertIsNone(counts["no_recipient"])
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)

    def test_misplaced_duplicate_is_checked_before_identity_deduplication(self):
        record, path = self._new_pending_file("dead-A")
        wrong_directory = self.state_root / "pending-delivery" / pending_delivery.recipient_digest(
            "other-A"
        )
        wrong_directory.mkdir(parents=True)
        misplaced = wrong_directory / path.name
        misplaced.write_bytes(path.read_bytes())
        before = {
            item: (item.read_bytes(), item.stat().st_mtime_ns)
            for item in (path, misplaced)
        }

        counts = self._count_with_complete_graph(("dead-A",))

        self.assertEqual(counts["pending"], 2)
        self.assertIsNone(counts["no_recipient"])
        self.assertEqual({
            item: (item.read_bytes(), item.stat().st_mtime_ns)
            for item in (path, misplaced)
        }, before)
        self.assertEqual(record["delivery_id"], path.stem)

    def test_unhashable_identity_fields_remain_unknown_without_raising(self):
        files = []
        malformed_rows = (
            ("malformed-A", "c", {"delivery_id": ["delivery-" + "d" * 32]}),
            ("malformed-B", "e", {"recipient_digest": {"invalid": "digest"}}),
        )
        for storage_sid, suffix, changes in malformed_rows:
            _record_value, path = self._new_pending_file(storage_sid, suffix)
            row = json.loads(path.read_text(encoding="utf-8"))
            row.update(changes)
            path.write_text(json.dumps(row), encoding="utf-8")
            files.append(path)
        before = {
            path: (path.read_bytes(), path.stat().st_mtime_ns)
            for path in files
        }

        counts = self._count_with_complete_graph(("malformed-A", "malformed-B"))

        self.assertEqual(counts["pending"], 2)
        self.assertIsNone(counts["no_recipient"])
        self.assertEqual({
            path: (path.read_bytes(), path.stat().st_mtime_ns)
            for path in files
        }, before)

    def test_live_successor_and_live_untidied_storage_are_excluded(self):
        successor = self._count_with_graph("dead-A", "live-C", live={("claude", "live-C")})
        self.assertEqual(successor["no_recipient"], 0)

    def test_live_untidied_storage_is_excluded(self):
        untidied = self._count_with_graph("live-A", "live-A", live={("claude", "live-A")})
        self.assertEqual(untidied["no_recipient"], 0)

    def test_unresolved_recipient_and_missing_census_remain_unknown(self):
        unresolved = self._count_with_graph("dead-A", None)
        self.assertIsNone(unresolved["no_recipient"])
        pending_delivery.create(**_record(root=self.state_root))
        missing = dispatch._pending_delivery_counts([str(self.jobs_path)])
        self.assertIsNone(missing["no_recipient"])

    def test_failed_empty_and_complete_empty_census_have_different_results(self):
        complete_empty = collectors._session_census_observation(
            procscan._CensusRows(complete=True), {"complete": True})
        failed_empty = collectors._session_census_observation(
            procscan._CensusRows(complete=False, reasons=("ps-error-OSError",)),
            {"complete": True})

        self.assertTrue(complete_empty["complete"])
        self.assertEqual(self._count_with_graph(
            "dead-A", "dead-C", session_observation=complete_empty)["no_recipient"], 1)
        self.assertFalse(failed_empty["complete"])
        self.assertEqual(self._count_with_graph(
            "dead-A", "dead-C", session_observation=failed_empty)["no_recipient"], None)

    def test_complete_observed_census_counts_dead_and_excludes_live_recipient(self):
        sessions = procscan._CensusRows(
            [SimpleNamespace(harness="claude", session_id="live-C")], complete=True)
        observation = collectors._session_census_observation(sessions, {"complete": True})

        dead = self._count_with_graph(
            "dead-A", "dead-C", session_observation=observation)
        live = self._count_with_graph(
            "live-A", "live-C", session_observation=observation,
            graph_sids=("dead-A", "live-A"),
            recipient_by_storage={"dead-A": "dead-C", "live-A": "live-C"})

        self.assertEqual((observation["complete"], observation["live"]),
                         (True, frozenset({("claude", "live-C")})))
        self.assertEqual(dead["no_recipient"], 1)
        self.assertEqual(live["pending"], 2)
        self.assertEqual(live["no_recipient"], 1)

    def test_process_and_native_sid_gaps_with_complete_herdr_stay_unknown(self):
        incomplete_scans = (
            procscan._CensusRows(complete=False, reasons=("effective-uid-unavailable",)),
            procscan._CensusRows(complete=False, reasons=("ps-error-OSError",)),
            procscan._CensusRows(complete=False, reasons=("ps-nonzero-exit",)),
            procscan._CensusRows(complete=False, reasons=("ps-timeout",)),
            procscan._CensusRows(complete=False, reasons=("process-row-malformed",)),
            procscan._CensusRows(complete=False, reasons=("process-row-pid-invalid",)),
            procscan._CensusRows(complete=False, reasons=("harness-filter-partial",)),
        )
        for rows in incomplete_scans:
            with self.subTest(reason=rows.census_reasons):
                observation = collectors._session_census_observation(rows, {"complete": True})
                self.assertTrue(observation["herdr_complete"])
                self.assertFalse(observation["process_complete"])
                self.assertIsNone(self._count_with_graph(
                    "dead-A", "dead-C", session_observation=observation)["no_recipient"])

        unknown_sid = procscan._CensusRows(
            [SimpleNamespace(harness="claude", session_id=None)], complete=True)
        observation = collectors._session_census_observation(unknown_sid, {"complete": True})
        self.assertTrue(observation["process_complete"])
        self.assertFalse(observation["native_sid_complete"])
        self.assertIsNone(self._count_with_graph(
            "dead-A", "dead-C", session_observation=observation)["no_recipient"])

        missing_herdr = collectors._session_census_observation(
            procscan._CensusRows(complete=True), {"complete": False})
        self.assertIsNone(self._count_with_graph(
            "dead-A", "dead-C", session_observation=missing_herdr)["no_recipient"])

    def test_complete_census_counts_only_dead_recipients_and_preserves_sources(self):
        dead = pending_delivery.create(**_record(root=self.state_root,
                                                  recipient_key="dead-A"))
        expired = pending_delivery.create(**_record(
            root=self.state_root, recipient_key="expired-A",
            delivery_id="delivery-" + "c" * 32))
        pending_delivery.expire_if_due(
            self.state_root, "expired-A", expired["delivery_id"],
            actor="dispatch-reconcile", reason="recipient-session-gone", liveness="known")
        self._write_jobs_row({
            "attempt_id": "att-census-source",
            "delivery_intent": "1",
            "parent_sid": "dead-A",
            "delivery_id": dead["delivery_id"],
        })
        obligations = self.state_root / "supervisor-state" / "obligations"
        obligations.mkdir(parents=True, exist_ok=True)
        retained = obligations / ("batch-" + "d" * 32 + ".json")
        retained.write_text(json.dumps({"schema_version": 1, "state": "unknown"}),
                            encoding="utf-8")
        before_jobs = (self.jobs_path.read_bytes(), self.jobs_path.stat().st_mtime_ns)
        json_paths = sorted(self.state_root.rglob("*.json"))
        before_json = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in json_paths}
        observation = collectors._session_census_observation(
            procscan._CensusRows(complete=False, reasons=("ps-timeout",)),
            {"complete": True})

        counts = dispatch._pending_delivery_counts(
            [str(self.jobs_path)], session_observation=observation)

        self.assertEqual(counts, {"pending": 1, "expired": 1, "retained_batch_duties": 1,
                                  "no_recipient": None})
        self.assertEqual((self.jobs_path.read_bytes(), self.jobs_path.stat().st_mtime_ns),
                         before_jobs)
        self.assertEqual({path: (path.read_bytes(), path.stat().st_mtime_ns)
                          for path in json_paths}, before_json)
        self.assertEqual(pending_delivery.read(
            self.state_root, "dead-A", dead["delivery_id"])["state"], "pending")
        self.assertEqual(pending_delivery.read(
            self.state_root, "expired-A", expired["delivery_id"])["state"], "expired")

    def _registered_registry_fixture(self, root, filename, suffix, *, target="claude", same_sid=False,
                                     legacy_empty_registry=False):
        import dispatch_seat_handover as handover
        import session_tidy as tidy
        from route_identity import route_hash
        root.mkdir(parents=True, exist_ok=True)
        jobs = root / filename
        sid, aid, did = "storage-" + suffix, "att-registry-" + suffix, "delivery-registry-" + suffix
        holder = sid if same_sid else "holder-" + suffix
        route = {"route_id": "rt-registry-" + suffix, "parent_cycle_id": "cycle-" + suffix,
                 "nodes": [{"id": "frame"}]}
        route["route_hash"] = route_hash(route)
        route_path = root / "route.json"; route_path.write_text(json.dumps(route))
        receipt = {"schema_version": 2, "state": "ready", "parent_attempt_id": "att-parent",
            "job_registry": "" if legacy_empty_registry else str(jobs),
            "delivery_classification": "success", "children": [{
                "attempt_id": aid, "status": "done", "readiness": "ready", "reason": "terminal-complete",
                "required_action": "advance-completed", "harness": "claude", "delivery_classification": "success"}]}
        record = pending_delivery.create(root, recipient_kind="claude-parent-runtime", recipient_key=sid,
            delivery_id=did, session_generation="", session_generation_supported="0", attempt_ids=[aid],
            parent_attempt_id="att-parent", route_id=route["route_id"], route_node="frame", receipt=receipt,
            receipt_digest=pending_delivery._canonical_receipt_digest(receipt), row_revisions={aid: "revision"})
        meta = {"attempt_id": aid, "attempt_schema_version": "2", "dispatch_depth": "1",
            "registered_worker": "1", "worker_type": "frame", "parent_sid": sid, "parent_harness": "claude",
            "parent_completion_delivery": "claude-parent-runtime", "delivery_intent": "1",
            "delivery_id": did, "delivery_receipt_digest": record["receipt_digest"],
            "delivery_row_revision": "revision", "failure_class": "pass", "note": "completed-marker",
            "launch_outcome": "reaped-before-publish", "route_id": route["route_id"],
            "route_file": str(route_path), "route_hash": route["route_hash"], "route_node": "frame"}
        jobs.write_text("now\tdone\t/fixture/repo\t/fixture/wt\tframe\t" +
                        ",".join(key + "=" + value for key, value in meta.items()) + "\n")
        seat = tidy.Seat("pane", "registry-" + suffix)
        tidy._append_ledger_entry(seat, dict(event="start", harness="claude", sid=sid, ts=1))
        tidy._append_ledger_entry(seat, dict(event="handover", source="retire", harness=target,
            sid=holder, **{"from": {"harness": "claude", "sid": sid}}, ts=2,
            bindings=[handover.binding_of(jobs, meta)]))
        self.assertEqual(handover.record_recipient_binding(record, sid, harness="claude", jobs=jobs),
                         (target, holder))
        return jobs, record, sid, (target, holder)

    def _observed_registry_count(self, paths, live):
        before = {path: (path.read_bytes(), path.stat().st_mtime_ns)
                  for path in self.state_root.rglob("*") if path.is_file()}
        observation = dict(complete=True, herdr_complete=True, process_complete=True,
                           native_sid_complete=True, live=live)
        counts = dispatch._pending_delivery_counts(paths, session_observation=observation)
        self.assertEqual({path: (path.read_bytes(), path.stat().st_mtime_ns)
                          for path in self.state_root.rglob("*") if path.is_file()}, before)
        return counts

    def _intent_fixture(self, suffix, *, filename="original-jobs.tsv", **kwargs):
        jobs, record, sid, holder = self._registered_registry_fixture(
            self.state_root / suffix, filename, suffix, **kwargs)
        pending_delivery.record_path(jobs.parent, sid, record["delivery_id"]).unlink()
        return jobs, record, sid, holder

    def _replace_registry_meta(self, jobs, **changes):
        fields = jobs.read_text().splitlines()[-1].split("\t")
        meta = dict(part.split("=", 1) for part in fields[5].split(",") if "=" in part)
        for key, value in changes.items():
            if value is None:
                meta.pop(key, None)
            else:
                meta[key] = value
        fields[5] = ",".join(key + "=" + value for key, value in meta.items())
        jobs.write_text("\t".join(fields) + "\n")
        return meta

    def test_intent_only_original_registries_and_defaults_resolve_current_holder(self):
        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(self.state_root / "history")}):
            first = self._intent_fixture("intent-one")
            second = self._intent_fixture("intent-two", filename="another-registry.txt")
            default = self.state_root / "default.log"
            for data in (b"", first[0].read_bytes()):
                default.write_bytes(data)
                with mock.patch.dict(os.environ, {"AGENT_DISPATCH_JOBS": str(default)}):
                    alias = first[0].parent / ".." / first[0].parent.name / first[0].name
                    for paths in ([first[0], second[0]], [second[0], alias, first[0]]):
                        with self.subTest(default=data, paths=paths):
                            dead = self._observed_registry_count(paths,
                                {("claude", first[2]), ("claude", second[2])})
                            self.assertEqual(dead, dict(pending=2, expired=0,
                                retained_batch_duties=0, no_recipient=2))
                            live = self._observed_registry_count(paths, {first[3], second[3]})
                            self.assertEqual(live["no_recipient"], 0)
                            self.assertEqual(live["pending"], 2)

    def test_intent_cross_harness_same_sid_then_qualified_successor(self):
        import session_tidy as tidy
        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(self.state_root / "history")}):
            jobs, _, sid, holder = self._intent_fixture("intent-pair", target="codex", same_sid=True)
            self.assertEqual(self._observed_registry_count([jobs], {("claude", sid)})["no_recipient"], 1)
            self.assertEqual(self._observed_registry_count([jobs], {holder})["no_recipient"], 0)
            seat = tidy.Seat("pane", "intent-successor")
            tidy._append_ledger_entry(seat, dict(event="succession", source="tidy-clear",
                harness="codex", sid="successor", **{"from": {"harness": "codex", "sid": sid}}, ts=3))
            self.assertEqual(self._observed_registry_count([jobs], {holder})["no_recipient"], 1)
            self.assertEqual(self._observed_registry_count([jobs], {("codex", "successor")})["no_recipient"], 0)

    def test_intent_missing_identity_and_route_stay_unknown_beside_live_parent(self):
        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(self.state_root / "history")}):
            good = self._intent_fixture("intent-good")
            for field in ("attempt_id", "parent_sid", "delivery_id", "parent_harness", "route_id", "route_hash"):
                bad = self._intent_fixture("intent-missing-" + field)
                changes = {field: None}
                if field == "parent_harness":
                    changes.update(parent_completion_delivery=None, main_runtime_harness=None, harness="codex")
                self._replace_registry_meta(bad[0], **changes)
                with self.subTest(field=field):
                    counts = self._observed_registry_count([good[0], bad[0]], {good[3], bad[3]})
                    self.assertIsNone(counts["no_recipient"])
                    self.assertEqual(counts["pending"], 2)

    def test_intent_legacy_parent_carrier_and_latest_row_are_used(self):
        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(self.state_root / "history")}):
            jobs, _, sid, holder = self._intent_fixture("intent-legacy")
            meta = self._replace_registry_meta(jobs, parent_harness=None, harness="codex",
                                               attempt_schema_version=None, registered_worker=None)
            self.assertEqual(self._observed_registry_count([jobs], {holder})["no_recipient"], 0)
            self.assertEqual(self._observed_registry_count([jobs], {("claude", sid)})["no_recipient"], 1)
            original = jobs.read_bytes()
            meta["parent_harness"] = "codex"
            stale = "now\tdone\t/r\t/w\ts\t" + ",".join(k + "=" + v for k, v in meta.items()) + "\n"
            jobs.write_bytes(stale.encode() + original)
            self.assertEqual(self._observed_registry_count([jobs], {holder})["no_recipient"], 0)

    def test_intent_registry_gaps_and_recovery_do_not_hide_behind_live_holder(self):
        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(self.state_root / "history")}):
            good = self._intent_fixture("registry-good")
            bad = self._intent_fixture("registry-bad")
            original = bad[0].read_bytes()
            for extra in (b"partial", b"broken\n", b"\xff\n", b"now\tdone\t/r\t/w\ts\tdelivery_intent=1"):
                bad[0].write_bytes(original + extra)
                with self.subTest(extra=extra):
                    self.assertIsNone(self._observed_registry_count([good[0], bad[0]],
                                                                   {good[3], bad[3]})["no_recipient"])
                bad[0].write_bytes(original)
                self.assertEqual(self._observed_registry_count([good[0], bad[0]],
                                                               {good[3], bad[3]})["no_recipient"], 0)
            real_view = dispatch._dispatch_registry_cache.registry_view
            for error in (PermissionError("private registry unreadable"), FileNotFoundError("private registry missing")):
                with mock.patch.object(dispatch._dispatch_registry_cache, "registry_view",
                        side_effect=lambda path, **kw: (_ for _ in ()).throw(error)
                        if Path(path) == bad[0] else real_view(path, **kw)):
                    self.assertIsNone(self._observed_registry_count([good[0], bad[0]],
                                                                   {good[3], bad[3]})["no_recipient"])
                self.assertEqual(self._observed_registry_count([good[0], bad[0]],
                                                               {good[3], bad[3]})["no_recipient"], 0)

    def test_intent_lineage_gaps_and_recovery_leave_inputs_unchanged(self):
        import session_tidy as tidy
        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(self.state_root / "history")}):
            jobs, _, _, holder = self._intent_fixture("lineage-intent")
            ledger = tidy._ledger_path(tidy.Seat("pane", "registry-lineage-intent"))
            original = ledger.read_bytes()
            for extra in (b'{"event":"succession",', b'[]\n', b'\xff\n'):
                ledger.write_bytes(original + extra)
                self.assertIsNone(self._observed_registry_count([jobs], {holder})["no_recipient"])
                ledger.write_bytes(original)
                self.assertEqual(self._observed_registry_count([jobs], {holder})["no_recipient"], 0)
            real_read = tidy.read_bytes
            with mock.patch.object(tidy, "read_bytes", side_effect=lambda p: None if p == ledger else real_read(p)):
                import dispatch_seat_handover as handover
                handover._LINEAGE_GRAPH_CACHE.clear()
                self.assertIsNone(self._observed_registry_count([jobs], {holder})["no_recipient"])
            self.assertEqual(self._observed_registry_count([jobs], {holder})["no_recipient"], 0)

    def test_intent_incomplete_census_remains_unknown(self):
        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(self.state_root / "history")}):
            jobs, _, _, holder = self._intent_fixture("census-intent")
            complete = dict(complete=True, herdr_complete=True, process_complete=True,
                            native_sid_complete=True, live={holder})
            for field in ("complete", "herdr_complete", "process_complete", "native_sid_complete"):
                for value in (None, False):
                    with self.subTest(field=field, value=value):
                        self.assertIsNone(dispatch._pending_delivery_counts([jobs],
                            session_observation={**complete, field: value})["no_recipient"])
            self.assertIsNone(dispatch._pending_delivery_counts([jobs])["no_recipient"])
            self.assertEqual(self._observed_registry_count([jobs], {holder})["no_recipient"], 0)

    def test_intent_dedup_checks_every_root_authority_in_both_orders(self):
        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(self.state_root / "history")}):
            jobs, record, sid, holder = self._intent_fixture("intent-original")
            other = self.state_root / "intent-copy"; other.mkdir()
            copied = other / "copied.tsv"; copied.write_bytes(jobs.read_bytes())
            for paths in ([jobs, copied], [copied, jobs]):
                with self.subTest(paths=paths):
                    counts = self._observed_registry_count(paths, {holder, ("claude", sid)})
                    self.assertEqual(counts["pending"], 1)
                    self.assertIsNone(counts["no_recipient"])
            # Identical authority with no handover: two roots, one delivery.
            self._replace_registry_meta(jobs, route_id="rt-untidied")
            copied.write_bytes(jobs.read_bytes())
            for paths in ([jobs, copied], [copied, jobs], [jobs, jobs]):
                counts = self._observed_registry_count(paths, set())
                self.assertEqual(counts["pending"], 1)
                self.assertEqual(counts["no_recipient"], 1)
            self._replace_registry_meta(copied, parent_harness=None, parent_completion_delivery=None)
            for paths in ([jobs, copied], [copied, jobs]):
                self.assertIsNone(self._observed_registry_count(paths, {("claude", sid)})["no_recipient"])

    def test_intent_and_materialized_duplicates_preserve_unknown_bodies(self):
        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(self.state_root / "history")}):
            jobs, record, sid, holder = self._registered_registry_fixture(
                self.state_root / "materialized-intent", "named.tsv", "mixed")
            path = pending_delivery.record_path(jobs.parent, sid, record["delivery_id"])
            original = path.read_bytes()
            self.assertEqual(self._observed_registry_count([jobs, jobs], set())["pending"], 1)
            self.assertEqual(self._observed_registry_count([jobs], set())["no_recipient"], 1)
            self.assertEqual(self._observed_registry_count([jobs], {holder})["no_recipient"], 0)

            for data in (b"not json", json.dumps({**record, "attempt_ids": ["att-unknown"]}).encode()):
                path.write_bytes(data)
                self.assertIsNone(self._observed_registry_count([jobs], {holder})["no_recipient"])
            path.write_bytes(original)
            self.assertEqual(self._observed_registry_count([jobs], {holder})["no_recipient"], 0)

    def test_invalid_pending_receipt_beside_live_intent_remains_unknown(self):
        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(self.state_root / "history")}):
            good = self._intent_fixture("live-with-corruption")
            jobs, record, sid, holder = self._registered_registry_fixture(
                self.state_root / "corrupt-body", "jobs.tsv", "corrupt-body")
            path = pending_delivery.record_path(jobs.parent, sid, record["delivery_id"])
            for receipt in ([], ["malformed"], True, "invalid"):
                path.write_text(json.dumps({**record, "receipt": receipt}))
                with self.subTest(receipt=receipt):
                    self.assertIsNone(self._observed_registry_count([good[0], jobs],
                                                                   {good[3], holder})["no_recipient"])

    def test_duplicate_legacy_bodies_are_resolved_before_absence_dedup(self):
        import session_tidy as tidy
        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(self.state_root / "history")}):
            sid = "legacy-duplicate"
            tidy._append_ledger_entry(tidy.Seat("pane", "legacy-duplicate"),
                                     dict(event="start", harness="claude", sid=sid, ts=1))
            receipt = _record()["receipt"]
            receipt = {**receipt, "job_registry": ""}
            jobs_paths, records = [], []
            for name in ("first-legacy", "second-legacy"):
                root = self.state_root / name; root.mkdir()
                jobs = root / "named.tsv"
                jobs.write_text("now\tdone\t/r\t/w\ts\tattempt_id=" + receipt["children"][0]["attempt_id"] +
                                ",parent_sid=" + sid + ",parent_harness=claude\n")
                record = pending_delivery.create(**_record(root=root, recipient_key=sid, receipt=receipt,
                    receipt_digest=pending_delivery._canonical_receipt_digest(receipt)))
                jobs_paths.append(jobs)
                records.append(pending_delivery.record_path(root, sid, record["delivery_id"]))
            for paths in (jobs_paths, jobs_paths[::-1]):
                counts = self._observed_registry_count(paths, set())
                self.assertEqual(counts["no_recipient"], 1)
                self.assertEqual(counts["pending"], 2)  # Existing physical pending count.
                self.assertEqual(self._observed_registry_count(paths, {("claude", sid)})["no_recipient"], 0)
            changed = json.loads(records[1].read_text()); changed["route_id"] = "rt-other"
            records[1].write_text(json.dumps(changed))
            for paths in (jobs_paths, jobs_paths[::-1]):
                self.assertIsNone(self._observed_registry_count(paths, {("claude", sid)})["no_recipient"])

    def test_intent_duplicate_metadata_conflict_is_unknown_even_with_same_live_holder(self):
        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(self.state_root / "history")}):
            jobs, _, sid, _ = self._intent_fixture("same-holder-conflict")
            self._replace_registry_meta(jobs, route_id="rt-unbound")
            root = self.state_root / "same-holder-copy"; root.mkdir()
            copied = root / "other.tsv"; copied.write_bytes(jobs.read_bytes())
            self._replace_registry_meta(copied, delivery_row_revision="different-revision")
            for paths in ([jobs, copied], [copied, jobs]):
                counts = self._observed_registry_count(paths, {("claude", sid)})
                self.assertEqual(counts["pending"], 1)
                self.assertIsNone(counts["no_recipient"])

    def test_intent_cycle_is_unknown_and_expired_and_retained_counts_are_preserved(self):
        import session_tidy as tidy
        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(self.state_root / "history")}):
            jobs, record, sid, holder = self._intent_fixture("cycle-intent")
            seat = tidy.Seat("pane", "cycle-intent")
            for origin, target in ((holder[1], "B"), ("B", holder[1])):
                tidy._append_ledger_entry(seat, dict(event="succession", source="tidy-clear",
                    harness=holder[0], sid=target, **{"from": {"harness": holder[0], "sid": origin}}, ts=3))
            self.assertIsNone(self._observed_registry_count([jobs], {holder})["no_recipient"])
            # Terminal records suppress their matching intent; retained duties
            # and expired records remain independent of the absence census.
            record = pending_delivery.create(**_record(root=self.state_root, recipient_key="expired",
                                                       delivery_id="delivery-expired"))
            pending_delivery.expire_if_due(self.state_root, "expired", record["delivery_id"],
                actor="dispatch-reconcile", reason="recipient-session-gone", liveness="known")
            duty = self.state_root / "supervisor-state/obligations/batch-fixture.json"
            duty.parent.mkdir(parents=True)
            duty.write_text(json.dumps(dict(schema_version=1, id="batch-fixture", state="observing")))
            counts = self._observed_registry_count([jobs, self.jobs_path], {holder})
            self.assertEqual((counts["pending"], counts["expired"], counts["retained_batch_duties"]), (1, 1, 1))
            self.assertIsNone(counts["no_recipient"])

    def test_observed_intent_survives_later_metadata_and_partial_tail(self):
        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(self.state_root / "history")}):
            jobs, _, _, holder = self._intent_fixture("historical-intent")
            original = jobs.read_bytes()
            self._replace_registry_meta(jobs, delivery_intent=None)
            latest = jobs.read_bytes()
            jobs.write_bytes(original + latest)
            counts = self._observed_registry_count([jobs], {holder})
            self.assertEqual(counts["pending"], 1)
            self.assertEqual(counts["no_recipient"], 0)
            jobs.write_bytes(original.rstrip(b"\n"))
            counts = self._observed_registry_count([jobs], {holder})
            self.assertEqual(counts["pending"], 1)
            self.assertIsNone(counts["no_recipient"])

    def test_unreadable_pending_body_does_not_become_known_from_live_intent(self):
        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(self.state_root / "history")}):
            jobs, record, sid, holder = self._registered_registry_fixture(
                self.state_root / "unreadable-pending", "jobs.tsv", "unreadable-pending")
            path = pending_delivery.record_path(jobs.parent, sid, record["delivery_id"])
            read_text = Path.read_text
            def unreadable(candidate, *args, **kwargs):
                if candidate == path:
                    raise PermissionError("private pending body unavailable")
                return read_text(candidate, *args, **kwargs)
            with mock.patch.object(Path, "read_text", new=unreadable):
                self.assertIsNone(self._observed_registry_count([jobs], {holder})["no_recipient"])
            self.assertEqual(self._observed_registry_count([jobs], {holder})["no_recipient"], 0)

    def test_intent_fork_cycle_and_manual_rows_use_common_lineage_policy(self):
        import session_tidy as tidy
        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(self.state_root / "history")}):
            jobs, _, _, holder = self._intent_fixture("ambiguous-intent")
            seat = tidy.Seat("pane", "ambiguous-intent")
            for target in ("B", "C"):
                tidy._append_ledger_entry(seat, dict(event="succession", source="tidy-clear",
                    harness=holder[0], sid=target, **{"from": {"harness": holder[0], "sid": holder[1]}}, ts=3))
            self.assertIsNone(self._observed_registry_count([jobs], {holder})["no_recipient"])
            manual = self._intent_fixture("manual-intent")
            tidy._append_ledger_entry(tidy.Seat("pane", "manual-intent"), dict(event="handover",
                source="manual-clear", harness="claude", sid="unqualified",
                **{"from": {"harness": manual[3][0], "sid": manual[3][1]}}, ts=5))
            self.assertEqual(self._observed_registry_count([manual[0]], {("claude", "unqualified")})["no_recipient"], 1)
            self.assertEqual(self._observed_registry_count([manual[0]], {manual[3]})["no_recipient"], 0)

    def test_collected_intent_counts_reach_existing_render_projections(self):
        from fleet import render
        from fleet.explorer.live import build_view_model
        from fleet.explorer.grid import CellGrid
        from fleet.explorer.resources import draw_system
        from fleet.explorer.chrome import draw_chrome
        from fleet.explorer.state import NavState
        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(self.state_root / "history")}):
            jobs, _, _, holder = self._intent_fixture("render-intent")
            for counts in (self._observed_registry_count([jobs], set()),
                           self._observed_registry_count([jobs], {holder}),
                           dispatch._pending_delivery_counts([jobs])):
                vm = build_view_model({"sessions": [], "pending_delivery": counts}, now=0)
                grid = CellGrid(168, 44); draw_system(grid, vm)
                body = "\n".join(grid.rows_text())
                draw_chrome(grid, vm, NavState(view="SYSTEM"))
                for text in (body, grid.rows_text()[-1], str(render._delivery_pending_rows(counts))):
                    if counts["no_recipient"] is None:
                        self.assertNotIn("받을 세션 없음", text)
                    else:
                        self.assertIn(f"받을 세션 없음 {counts['no_recipient']}", text)

    def test_original_named_registries_override_empty_and_copied_defaults_read_only(self):
        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(self.state_root / "history")}):
            first = self._registered_registry_fixture(self.state_root / "one", "route-jobs.tsv", "one")
            second = self._registered_registry_fixture(self.state_root / "two", "another-registry.txt", "two")
            default = self.state_root / "copied-jobs.log"
            for data in (b"", first[0].read_bytes()):
                default.write_bytes(data)
                with mock.patch.dict(os.environ, {"AGENT_DISPATCH_JOBS": str(default)}):
                    live = {("claude", first[2]), ("claude", second[2])}
                    self.assertEqual(self._observed_registry_count([first[0], second[0]], live)["no_recipient"], 2)
                    self.assertEqual(self._observed_registry_count([first[0], second[0]],
                                     {first[3], second[3]})["no_recipient"], 0)
                    original = first[0].read_bytes(); first[0].unlink()
                    self.assertIsNone(self._observed_registry_count([first[0], second[0]], live)["no_recipient"])
                    first[0].write_bytes(original)
                    self.assertEqual(self._observed_registry_count([first[0], second[0]], live)["no_recipient"], 2)

    def test_legacy_empty_registry_keeps_the_observed_filename_over_copied_default(self):
        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(self.state_root / "history")}):
            jobs, record, sid, holder = self._registered_registry_fixture(
                self.state_root / "legacy", "original-legacy.tsv", "legacy-empty",
                legacy_empty_registry=True)
            default = jobs.parent / "copied-default.log"; default.write_bytes(jobs.read_bytes())
            with mock.patch.dict(os.environ, {"AGENT_DISPATCH_JOBS": str(default)}):
                self.assertEqual(self._observed_registry_count([jobs], {("claude", sid)})["no_recipient"], 1)
                self.assertEqual(self._observed_registry_count([jobs], {holder})["no_recipient"], 0)

    def test_cross_harness_same_sid_and_every_duplicate_body_keep_original_registry(self):
        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(self.state_root / "history")}):
            jobs, record, sid, holder = self._registered_registry_fixture(
                self.state_root / "original", "original.tsv", "pair", target="codex", same_sid=True)
            default = self.state_root / "default.log"; default.write_bytes(jobs.read_bytes())
            with mock.patch.dict(os.environ, {"AGENT_DISPATCH_JOBS": str(default)}):
                self.assertEqual(self._observed_registry_count([jobs], {("claude", sid)})["no_recipient"], 1)
                self.assertEqual(self._observed_registry_count([jobs], {holder})["no_recipient"], 0)
                other = self.state_root / "duplicate"; other.mkdir()
                copied = other / "copied.tsv"; copied.write_bytes(jobs.read_bytes())
                # Same identity/body in another root has an explicitly conflicting original registry.
                duplicate = pending_delivery.record_path(other, sid, record["delivery_id"])
                duplicate.parent.mkdir(parents=True)
                duplicate.write_bytes(pending_delivery.record_path(jobs.parent, sid, record["delivery_id"]).read_bytes())
                for paths in ([jobs, copied], [copied, jobs]):
                    with self.subTest(paths=paths):
                        self.assertIsNone(self._observed_registry_count(paths, {holder})["no_recipient"])

    def test_route_free_notice_keeps_registry_free_absence_accounting(self):
        import peer_obligations
        import session_notice
        import session_tidy as tidy
        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(self.state_root / "history"),
                "AGENT_DISPATCH_JOBS": str(self.state_root / "unavailable.tsv")}), \
             mock.patch.object(peer_obligations, "ensure_normal_runner", return_value=False), \
             mock.patch.object(peer_obligations, "request_dispatch_reconciliation", return_value=False), \
             mock.patch.object(peer_obligations, "_proc_start_for_store", return_value="100001"):
            jobs = self.state_root / "unavailable.tsv"
            notice = session_notice.notify("claude", "route-free-storage", key="registry-free",
                                           subject="completed", text="done", jobs=jobs)
            retry = session_notice.notify("claude", "route-free-storage", key="registry-free",
                                          subject="completed", text="done", jobs=jobs)
            self.assertEqual(retry["delivery_id"], notice["delivery_id"])
            self.assertEqual(len(peer_obligations.ObligationStore(self.state_root).list(kinds={"run-notice"})), 1)
            tidy._append_ledger_entry(tidy.Seat("pane", "route-free"),
                                     dict(event="start", harness="claude", sid="route-free-storage", ts=1))
            self.assertFalse(jobs.exists())
            self.assertEqual(self._observed_registry_count([jobs], set())["no_recipient"], 1)
            self.assertEqual(self._observed_registry_count([jobs], {("claude", "route-free-storage")})["no_recipient"], 0)
            unknown = dispatch._pending_delivery_counts([str(jobs)], session_observation={"complete": False})
            self.assertEqual(unknown["pending"], 1)
            self.assertIsNone(unknown["no_recipient"])

    def test_render_and_overview_show_known_count_but_never_unknown_as_zero(self):
        from fleet import render
        from fleet.explorer.live import build_view_model
        from fleet.explorer.grid import CellGrid
        from fleet.explorer.resources import draw_system
        from fleet.explorer.chrome import draw_chrome
        from fleet.explorer.state import NavState

        # The current explorer displays delivery in SYSTEM, replacing overview.py.
        for count in (1, 0, None, True, "0"):
            with self.subTest(no_recipient=count):
                counts = {"pending": 2, "expired": 0, "no_recipient": count}
                snapshot = {"sessions": [], "pending_delivery": counts}
                before = json.dumps(snapshot, sort_keys=True)
                vm = build_view_model(snapshot, now=0)
                grid = CellGrid(168, 44)
                draw_system(grid, vm)
                body = "\n".join(grid.rows_text())
                draw_chrome(grid, vm, NavState(view="SYSTEM"))
                footer = grid.rows_text()[-1]
                classic = str(render._delivery_pending_rows(counts))
                if isinstance(count, int) and not isinstance(count, bool):
                    for text in (body, footer, classic):
                        self.assertIn(f"받을 세션 없음 {count}", text)
                    self.assertEqual(vm.system["peer_delivery"]["no_recipient"], count)
                else:
                    for text in (body, footer, classic):
                        self.assertNotIn("받을 세션 없음", text)
                    self.assertNotIn("no_recipient", vm.system["peer_delivery"])
                self.assertEqual(json.dumps(snapshot, sort_keys=True), before)


if __name__ == "__main__":
    unittest.main()
