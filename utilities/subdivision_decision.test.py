#!/usr/bin/env python3
import importlib.util, json, tempfile, unittest, threading
from pathlib import Path
from unittest import mock

SPEC = importlib.util.spec_from_file_location("subdivision_decision", Path(__file__).with_name("subdivision_decision.py"))
D = importlib.util.module_from_spec(SPEC); SPEC.loader.exec_module(D)

class DecisionLedgerTest(unittest.TestCase):
    def test_first_wins_and_inventory_states(self):
        with tempfile.TemporaryDirectory() as td:
            jobs = Path(td) / "jobs.log"; jobs.write_text("")
            route = {"route_id":"rt-test","route_hash":"h","capability":"autopilot-code","effective_intensity":"standard"}
            node = {"id":"execute","subdivision":{"min_intensity":"standard","max_slices":4,"disjointness":"exact-fixed-files"}}
            ctx = D.lookup(route, node, jobs=jobs)
            self.assertTrue(D.commit(ctx, "admitted", "", "m", 2)["appended"])
            self.assertTrue(D.commit(ctx, "admitted", "", "other", 3)["duplicate"])
            self.assertEqual(D.inventory(ctx)["rows"][0]["manifest_sha256"], "m")
    def test_closed_pairs_and_absence_are_distinct(self):
        with tempfile.TemporaryDirectory() as td:
            jobs = Path(td) / "jobs.log"; jobs.write_text("")
            ctx = D.lookup({"route_id":"rt-test","route_hash":"h"},{"id":"execute"},jobs=jobs)
            self.assertEqual(D.inventory(ctx)["health"], "absent")
            with self.assertRaises(D.DecisionError): D.commit(ctx, "admitted", "plan-declared-no-slices")

    def test_all_closed_pairs_and_event_id_are_deterministic(self):
        route = {"route_id": "rt-pairs", "route_hash": "h", "capability": "cap",
                 "requested_intensity": "standard", "effective_intensity": "standard"}
        node = {"id": "execute", "subdivision": {"min_intensity": "standard",
                "max_slices": 4, "disjointness": "exact-fixed-files"}}
        with tempfile.TemporaryDirectory() as td:
            jobs = Path(td) / "jobs"
            one, two = D.lookup(route, node, jobs=jobs), D.lookup(route, node, jobs=jobs)
            self.assertEqual(one["event_id"], two["event_id"])
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

    def test_first_decision_wins_and_concurrent_append_is_one_row(self):
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
            self.assertEqual(len(D.inventory(ctx)["rows"]), 1)
            duplicate = D.commit(ctx, "admitted", "", "other", 3)
            self.assertTrue(duplicate["duplicate"])
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

if __name__ == "__main__": unittest.main()
