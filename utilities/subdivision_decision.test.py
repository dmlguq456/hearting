#!/usr/bin/env python3
import importlib.util, json, tempfile, unittest, threading
from pathlib import Path
from unittest import mock

SPEC = importlib.util.spec_from_file_location("subdivision_decision", Path(__file__).with_name("subdivision_decision.py"))
D = importlib.util.module_from_spec(SPEC); SPEC.loader.exec_module(D)

class DecisionLedgerTest(unittest.TestCase):
    def test_first_decision_is_kept_and_a_different_one_is_typed_not_duplicate(self):
        with tempfile.TemporaryDirectory() as td:
            jobs = Path(td) / "jobs.log"; jobs.write_text("")
            route = {"route_id":"rt-test","route_hash":"h","capability":"autopilot-code","effective_intensity":"standard"}
            node = {"id":"execute","subdivision":{"min_intensity":"standard","max_slices":4,"disjointness":"exact-fixed-files"}}
            ctx = D.lookup(route, node, jobs=jobs)
            self.assertTrue(D.commit(ctx, "admitted", "", "m", 2)["appended"])
            # exact replay of the same decision: one record, no second row
            replay = D.commit(ctx, "admitted", "", "m", 2)
            self.assertTrue(replay["duplicate"]); self.assertFalse(replay["appended"])
            # a *different* record under the same event is a conflict, never a
            # silent duplicate PASS -- and it does not overwrite the first
            conflict = D.commit(ctx, "admitted", "", "other", 3)
            self.assertTrue(conflict["conflict"]); self.assertTrue(conflict["appended"])
            self.assertFalse(conflict["duplicate"])
            self.assertEqual(conflict["warning"], "subdivision-decision-conflict")
            rows = D.inventory(ctx)["rows"]
            self.assertEqual(rows[0]["manifest_sha256"], "m")
            self.assertEqual(rows[0]["decision"], "admitted")
            self.assertNotIn("phase", rows[0])
            self.assertEqual(rows[1]["phase"], 1)
            self.assertEqual(len(rows), 2)

    def test_closed_pairs_and_absence_are_distinct(self):
        with tempfile.TemporaryDirectory() as td:
            jobs = Path(td) / "jobs.log"; jobs.write_text("")
            ctx = D.lookup({"route_id":"rt-test","route_hash":"h"},{"id":"execute"},jobs=jobs)
            self.assertEqual(D.inventory(ctx)["health"], "absent")
            self.assertFalse(D.inventory(ctx)["inventory_complete"])
            with self.assertRaises(D.DecisionError): D.commit(ctx, "admitted", "plan-declared-no-slices")

    def test_event_id_is_fixed_per_lookup_and_distinct_across_actions(self):
        route = {"route_id": "rt-pairs", "route_hash": "h", "capability": "cap",
                 "requested_intensity": "standard", "effective_intensity": "standard"}
        node = {"id": "execute", "subdivision": {"min_intensity": "standard",
                "max_slices": 4, "disjointness": "exact-fixed-files"}}
        with tempfile.TemporaryDirectory() as td:
            jobs = Path(td) / "jobs"
            one, two = D.lookup(route, node, jobs=jobs), D.lookup(route, node, jobs=jobs)
            # two separate lookups are two separate actions (a refused plan and
            # a corrected retry must not collide on one event)
            self.assertNotEqual(one["event_id"], two["event_id"])
            # ... and one lookup's id does not move for the rest of that action
            self.assertEqual(one["event_id"], D.lookup(
                route, node, jobs=jobs, action_identity=one["action_identity"])["event_id"])
            valid = [("not-eligible", "subdivision-not-permitted"),
                     ("considered-declined", "plan-declared-no-slices"),
                     ("refused", "fixed-file-overlap"), ("admitted", "")]
            for decision, reason in valid:
                context = dict(one)
                context["event_id"] = decision + "-event"
                self.assertTrue(D.commit(context, decision, reason)["appended"])
            for decision, reason in valid:
                bad = dict(one)
                with self.assertRaises(D.DecisionError): D.commit(bad, decision, "" if reason else "fixed-file-overlap")
            self.assertEqual(len(D.inventory(one)["rows"]), len(valid))

    def test_concurrent_identical_commit_appends_one_row(self):
        with tempfile.TemporaryDirectory() as td:
            jobs = Path(td) / "jobs"
            ctx = D.lookup({"route_id": "rt-race", "route_hash": "h"}, {"id": "execute"}, jobs=jobs)
            results = []
            barrier = threading.Barrier(2)
            def commit_once():
                barrier.wait()
                results.append(D.commit(ctx, "admitted", "", "m", 2))
            threads = [threading.Thread(target=commit_once) for _ in range(2)]
            for thread in threads: thread.start()
            for thread in threads: thread.join()
            self.assertEqual(sum(item["appended"] for item in results), 1)
            self.assertEqual(sum(item["duplicate"] for item in results), 1)
            self.assertEqual(len(D.inventory(ctx)["rows"]), 1)
            self.assertEqual(D.inventory(ctx)["rows"][0]["manifest_sha256"], "m")

    def test_inventory_health_and_fail_open_warning(self):
        with tempfile.TemporaryDirectory() as td:
            jobs = Path(td) / "jobs"
            ctx = D.lookup({"route_id": "rt-health", "route_hash": "h"}, {"id": "execute"}, jobs=jobs)
            self.assertEqual(D.inventory(ctx)["health"], "absent")
            path = Path(ctx["ledger_path"])
            path.parent.mkdir(parents=True)
            path.write_text('{"event_id":"partial"}\n{"broken"', encoding="utf-8")
            corrupt = D.inventory(ctx)
            self.assertEqual(corrupt["health"], "corrupt")
            self.assertFalse(corrupt["inventory_complete"])
            with mock.patch.object(Path, "read_text", side_effect=OSError("denied")):
                self.assertEqual(D.inventory(ctx)["health"], "unreadable")
            with mock.patch.object(D.os, "open", side_effect=OSError("denied")):
                warning = D.commit(ctx, "admitted", "")
            self.assertEqual(warning["warning"], "subdivision-decision-unrecorded")

    def test_a_lost_record_is_not_reported_as_a_complete_inventory(self):
        """M-3: fail-open must not let a later query claim completeness. A
        readable ledger whose other rows parse is still incomplete once one
        append was lost, and that state is distinct from `absent`."""
        with tempfile.TemporaryDirectory() as td:
            jobs = Path(td) / "jobs"
            ctx_ok = D.lookup({"route_id": "rt-gap", "route_hash": "h"}, {"id": "execute"}, jobs=jobs)
            self.assertTrue(D.commit(ctx_ok, "admitted", "", "m", 2)["appended"])
            healthy = D.inventory(ctx_ok)
            self.assertEqual(healthy["health"], "healthy")
            self.assertTrue(healthy["inventory_complete"])
            ctx_lost = D.lookup({"route_id": "rt-gap", "route_hash": "h"}, {"id": "execute"}, jobs=jobs)
            with mock.patch.object(D.os, "open", side_effect=OSError("denied")):
                lost = D.commit(ctx_lost, "refused", "fixed-file-overlap")
            self.assertEqual(lost["warning"], "subdivision-decision-unrecorded")
            self.assertFalse(lost["appended"])
            after = D.inventory(ctx_ok)
            self.assertEqual(after["health"], "incomplete")
            self.assertFalse(after["inventory_complete"])
            self.assertEqual(after["warning"], "subdivision-decision-record-gap")
            self.assertEqual(len(after["rows"]), 1)
            self.assertEqual(after["gaps"][0]["event_id"], ctx_lost["event_id"])

    def test_gap_marker_survives_the_process_that_lost_the_record(self):
        """The durable marker, not just this process's memory, is what a later
        reader sees -- otherwise a lost record reads as complete tomorrow."""
        with tempfile.TemporaryDirectory() as td:
            jobs = Path(td) / "jobs"
            ctx = D.lookup({"route_id": "rt-durable-gap", "route_hash": "h"}, {"id": "execute"}, jobs=jobs)
            with mock.patch.object(D.os, "open", side_effect=OSError("denied")):
                D.commit(ctx, "admitted", "", "m", 2)
            gap_file = D._gap_path(Path(ctx["ledger_path"]))
            self.assertTrue(gap_file.is_file())
            recorded = json.loads(gap_file.read_text(encoding="utf-8").splitlines()[0])
            self.assertEqual(recorded["warning"], "subdivision-decision-unrecorded")
            D._UNRECORDED.clear()  # a fresh process has no memory of the loss
            fresh = D.inventory(ctx)
            self.assertEqual(fresh["health"], "incomplete")
            self.assertFalse(fresh["inventory_complete"])

if __name__ == "__main__": unittest.main()
