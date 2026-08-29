#!/usr/bin/env python3
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

UTILITIES = Path(__file__).resolve().parent
if str(UTILITIES) not in sys.path:
    sys.path.insert(0, str(UTILITIES))

import dispatch_launch_tuple as LT  # noqa: E402

ROUTE = {"route_id": "rt-lt", "route_hash": "sha256:route"}
NODE = {"id": "execute"}
TUPLE_KEY = "claude/headless/isolated/codex/conductor"


class RecordRejectionTest(unittest.TestCase):
    def test_row_schema_and_route_shard(self):
        with tempfile.TemporaryDirectory() as home:
            state_root = Path(home)
            result = LT.record_rejection(
                state_root, route=ROUTE, node=NODE, tuple_key=TUPLE_KEY,
                rejection_class="allocation-skip", evidence_ref="usage-claude",
                owner_attempt_id="att-owner",
            )
            self.assertIsInstance(result, Path)
            self.assertEqual(result, state_root / "launch-tuple" / "rt-lt.jsonl")
            row = json.loads(result.read_text(encoding="utf-8").strip())
            self.assertEqual(row["schema_version"], 1)
            self.assertEqual(row["route_id"], "rt-lt")
            self.assertEqual(row["route_node"], "execute")
            self.assertEqual(row["route_hash"], "sha256:route")
            self.assertEqual(row["owner_attempt_id"], "att-owner")
            self.assertEqual(row["tuple_key"], TUPLE_KEY)
            self.assertEqual(row["rejection_class"], "allocation-skip")
            self.assertEqual(row["evidence_ref"], "usage-claude")
            self.assertIsInstance(row["observed_at"], float)
            self.assertTrue(row["event_id"].startswith("lt-"))

    def test_unattributed_shard_when_route_id_missing(self):
        with tempfile.TemporaryDirectory() as home:
            state_root = Path(home)
            result = LT.record_rejection(
                state_root, route={}, node=NODE, tuple_key=TUPLE_KEY,
                rejection_class="candidate-unsupported", evidence_ref="status",
                owner_attempt_id="att-owner",
            )
            self.assertEqual(result, state_root / "launch-tuple" / "_unattributed.jsonl")

    def test_b47_10_rejection_class_enum_closed(self):
        with tempfile.TemporaryDirectory() as home:
            state_root = Path(home)
            result = LT.record_rejection(
                state_root, route=ROUTE, node=NODE, tuple_key=TUPLE_KEY,
                rejection_class="prior-unchanged-failure", evidence_ref="x",
                owner_attempt_id="att-owner",
            )
            self.assertEqual(result, ("launch-tuple-rejection-class-invalid",
                                       "unknown rejection_class='prior-unchanged-failure'"))
            self.assertFalse((state_root / "launch-tuple").exists())

    def test_b47_9_event_id_converges_across_three_invocations(self):
        with tempfile.TemporaryDirectory() as home:
            state_root = Path(home)
            for _ in range(3):
                result = LT.record_rejection(
                    state_root, route=ROUTE, node=NODE, tuple_key=TUPLE_KEY,
                    rejection_class="allocation-skip", evidence_ref="usage-claude",
                    owner_attempt_id="att-owner",
                )
                self.assertIsInstance(result, Path)
            path = state_root / "launch-tuple" / "rt-lt.jsonl"
            lines = path.read_text(encoding="utf-8").strip().splitlines()
            self.assertEqual(len(lines), 1, "same owner attempt must not grow the ledger")

    def test_owner_attempt_change_adds_exactly_one_row(self):
        with tempfile.TemporaryDirectory() as home:
            state_root = Path(home)
            for owner in ("att-1", "att-1", "att-2"):
                LT.record_rejection(
                    state_root, route=ROUTE, node=NODE, tuple_key=TUPLE_KEY,
                    rejection_class="allocation-skip", evidence_ref="usage-claude",
                    owner_attempt_id=owner,
                )
            path = state_root / "launch-tuple" / "rt-lt.jsonl"
            lines = path.read_text(encoding="utf-8").strip().splitlines()
            self.assertEqual(len(lines), 2)

    def test_event_id_formula_matches_spec(self):
        identity = ["rt-lt", "execute", TUPLE_KEY, "att-owner", "allocation-skip"]
        expected = "lt-" + hashlib.sha256(
            json.dumps(identity, ensure_ascii=False, separators=(",", ":")).encode()
        ).hexdigest()[:24]
        self.assertEqual(
            LT.rejection_event_id("rt-lt", "execute", TUPLE_KEY, "att-owner", "allocation-skip"),
            expected,
        )

    def test_b47_11_write_failure_typed_unspent_child_delta_zero(self):
        with tempfile.TemporaryDirectory() as home:
            state_root = Path(home)
            # A file where the ledger directory must go forces os.makedirs()
            # to fail regardless of the invoking user's privileges (root
            # bypasses permission bits, but not "not a directory").
            with open(state_root / "launch-tuple", "w", encoding="utf-8"):
                pass
            result = LT.record_rejection(
                state_root, route=ROUTE, node=NODE, tuple_key=TUPLE_KEY,
                rejection_class="allocation-skip", evidence_ref="usage-claude",
                owner_attempt_id="att-owner",
            )
            self.assertIsInstance(result, tuple)
            self.assertEqual(result[0], "launch-tuple-evidence-unrecorded")
            # record absence == unspent (§5.4) -- the verdict for this tuple
            # is conservative, and nothing about a launch decision changed.
            spent = LT.spent_tuples(state_root, "rt-lt", "execute", route_hash="sha256:route")
            self.assertNotIn(TUPLE_KEY, spent)

    def test_lock_contention_is_typed_not_a_crash(self):
        with tempfile.TemporaryDirectory() as home:
            state_root = Path(home)
            root = state_root / "launch-tuple"
            root.mkdir()
            lock_path = root / "rt-lt.jsonl.lock"
            holder = subprocess.Popen([sys.executable, "-c", (
                "import fcntl,time,sys; f=open(sys.argv[1],'a+'); "
                "fcntl.flock(f,fcntl.LOCK_EX); time.sleep(.5)"), str(lock_path)])
            try:
                time.sleep(.05)
                result = LT.record_rejection(
                    state_root, route=ROUTE, node=NODE, tuple_key=TUPLE_KEY,
                    rejection_class="allocation-skip", evidence_ref="usage-claude",
                    owner_attempt_id="att-owner",
                )
            finally:
                holder.wait(timeout=2)
            self.assertIsInstance(result, tuple)
            self.assertEqual(result[0], "launch-tuple-evidence-unrecorded")


class SpentTuplesTest(unittest.TestCase):
    def test_b47_10_spent_tuples_is_pure_query(self):
        with tempfile.TemporaryDirectory() as home:
            state_root = Path(home)
            LT.record_rejection(
                state_root, route=ROUTE, node=NODE, tuple_key=TUPLE_KEY,
                rejection_class="allocation-skip", evidence_ref="usage-claude",
                owner_attempt_id="att-owner",
            )
            path = state_root / "launch-tuple" / "rt-lt.jsonl"
            before = path.read_bytes()
            lock_before = (path.with_suffix(path.suffix + ".lock")).exists()
            for _ in range(3):
                spent = LT.spent_tuples(state_root, "rt-lt", "execute", route_hash="sha256:route")
                self.assertIn(TUPLE_KEY, spent)
            after = path.read_bytes()
            self.assertEqual(before, after, "spent_tuples() must never mutate the ledger file")
            # A pure query reads only its own arguments -- no ambient env or
            # process state may change its answer for the same inputs.
            with mock.patch.dict(os.environ, {"AGENT_DISPATCH_JOBS": "/nonexistent/jobs.log"}):
                same = LT.spent_tuples(state_root, "rt-lt", "execute", route_hash="sha256:route")
            self.assertEqual(set(same), set(spent))

    def test_absent_record_is_unspent(self):
        with tempfile.TemporaryDirectory() as home:
            state_root = Path(home)
            spent = LT.spent_tuples(state_root, "rt-lt", "execute", route_hash="sha256:route")
            self.assertEqual(spent, {})
            self.assertFalse((state_root / "launch-tuple").exists(),
                              "a pure query must never create the ledger directory")

    def test_route_hash_mismatch_is_unspent(self):
        with tempfile.TemporaryDirectory() as home:
            state_root = Path(home)
            LT.record_rejection(
                state_root, route=ROUTE, node=NODE, tuple_key=TUPLE_KEY,
                rejection_class="allocation-skip", evidence_ref="usage-claude",
                owner_attempt_id="att-owner",
            )
            spent = LT.spent_tuples(state_root, "rt-lt", "execute", route_hash="sha256:different")
            self.assertEqual(spent, {})

    def test_node_mismatch_is_unspent(self):
        with tempfile.TemporaryDirectory() as home:
            state_root = Path(home)
            LT.record_rejection(
                state_root, route=ROUTE, node=NODE, tuple_key=TUPLE_KEY,
                rejection_class="allocation-skip", evidence_ref="usage-claude",
                owner_attempt_id="att-owner",
            )
            spent = LT.spent_tuples(state_root, "rt-lt", "other-node", route_hash="sha256:route")
            self.assertEqual(spent, {})

    def test_malformed_row_is_ignored(self):
        with tempfile.TemporaryDirectory() as home:
            state_root = Path(home)
            root = state_root / "launch-tuple"
            root.mkdir()
            (root / "rt-lt.jsonl").write_text(
                json.dumps({"schema_version": 1, "route_id": "rt-lt", "route_node": "execute",
                            "route_hash": "sha256:route", "tuple_key": "only/four/fields/here"}) + "\n",
                encoding="utf-8",
            )
            spent = LT.spent_tuples(state_root, "rt-lt", "execute", route_hash="sha256:route")
            self.assertEqual(spent, {})


class ReportOnlyObservationTest(unittest.TestCase):
    def test_unarmed_write_report_is_a_noop(self):
        with tempfile.TemporaryDirectory() as home:
            state_root = Path(home)
            observation = LT.ReportOnlyObservation()
            result = LT.write_report(observation)
            self.assertIsNone(result)
            self.assertFalse((state_root / "launch-tuple").exists())

    def test_armed_write_report_writes_one_row_with_counters(self):
        with tempfile.TemporaryDirectory() as home:
            state_root = Path(home)
            observation = LT.ReportOnlyObservation()
            observation.arm(state_root, ROUTE, NODE, "att-owner")
            observation.universe = [TUPLE_KEY, "other/tuple/key/here/x"]
            observation.spent = {TUPLE_KEY: {}}
            observation.failed_tuples = frozenset()
            observation.note_unrecorded("launch-tuple-evidence-unrecorded")
            path = LT.write_report(observation)
            self.assertIsInstance(path, Path)
            row = json.loads(path.read_text(encoding="utf-8").strip())
            self.assertEqual(row["schema_version"], 1)
            self.assertEqual(row["route_id"], "rt-lt")
            self.assertEqual(row["route_node"], "execute")
            self.assertEqual(row["spent_seen"], 1)
            self.assertEqual(row["suppression_candidates"], 1)
            self.assertEqual(row["unrecorded"], 1)
            self.assertTrue(row["event_id"].startswith("ltr-"))

    def test_suppression_candidates_excludes_already_failed_tuples(self):
        with tempfile.TemporaryDirectory() as home:
            state_root = Path(home)
            observation = LT.ReportOnlyObservation()
            observation.arm(state_root, ROUTE, NODE, "att-owner")
            observation.universe = [TUPLE_KEY]
            observation.spent = {TUPLE_KEY: {}}
            observation.failed_tuples = frozenset({TUPLE_KEY})
            path = LT.write_report(observation)
            row = json.loads(path.read_text(encoding="utf-8").strip())
            self.assertEqual(row["spent_seen"], 1)
            self.assertEqual(row["suppression_candidates"], 0)

    def test_write_report_never_raises_on_lock_failure(self):
        with tempfile.TemporaryDirectory() as home:
            state_root = Path(home)
            observation = LT.ReportOnlyObservation()
            observation.arm(state_root, ROUTE, NODE, "att-owner")
            observation.universe = []
            observation.spent = {}
            (state_root / "launch-tuple").mkdir()
            # A file where `_report/` must go forces os.makedirs() to fail
            # regardless of the invoking user's privileges.
            with open(state_root / "launch-tuple" / "_report", "w", encoding="utf-8"):
                pass
            result = LT.write_report(observation)
            self.assertIsInstance(result, tuple)
            self.assertEqual(result[0], "launch-tuple-report-unrecorded")

    def test_report_written_to_report_shard_not_the_producer_ledger(self):
        with tempfile.TemporaryDirectory() as home:
            state_root = Path(home)
            LT.record_rejection(
                state_root, route=ROUTE, node=NODE, tuple_key=TUPLE_KEY,
                rejection_class="allocation-skip", evidence_ref="usage-claude",
                owner_attempt_id="att-owner",
            )
            observation = LT.ReportOnlyObservation()
            observation.arm(state_root, ROUTE, NODE, "att-owner")
            observation.universe = [TUPLE_KEY]
            observation.spent = {TUPLE_KEY: {}}
            LT.write_report(observation)
            ledger = (state_root / "launch-tuple" / "rt-lt.jsonl").read_text(encoding="utf-8")
            self.assertEqual(len(ledger.strip().splitlines()), 1, "producer ledger untouched by report writer")
            report = (state_root / "launch-tuple" / "_report" / "rt-lt.jsonl").read_text(encoding="utf-8")
            self.assertEqual(len(report.strip().splitlines()), 1)


class NoLiveStateLeakTest(unittest.TestCase):
    def test_module_never_writes_outside_the_given_state_root(self):
        # R4: the whole test module must resolve strictly under a tmp
        # state root -- never `~/.local/state/hearting/dispatch/`.
        live = Path.home() / ".local" / "state" / "hearting" / "dispatch" / "launch-tuple"
        self.assertFalse(live.exists() and any(live.iterdir()) if live.exists() else False)


if __name__ == "__main__":
    unittest.main()
