#!/usr/bin/env python3
"""Tests for moving settled registry rows to the dispatch archive (`dispatch_registry_archive.py`)."""
from collections import Counter
import fcntl
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import dispatch_registry_archive as A  # noqa: E402
import dispatch_registry_inventory as I  # noqa: E402

DAY = 86400 * 1_000_000_000
NOW = 1_800_000_000 * 1_000_000_000


def stamp(days_ago):
    return time.strftime("%Y-%m-%dT%H:%M:%S.000000Z", time.gmtime((NOW - days_ago * DAY) / 1e9))


def row(attempt, *, days_ago=30, status="done", route="rt-00000000000000a1", **extra):
    meta = {"attempt_schema_version": "2", "attempt_id": attempt, "route_id": route,
            "route_file": f"/routes/{route}.json", "route_hash": "h-" + route,
            "log_file": f"/nonexistent-p3b/logs/{attempt}.log", **extra}
    pipe = ",".join(f"{key}={value}" for key, value in meta.items())
    return f"{stamp(days_ago)}\t{status}\t/repo\t/repo\tslug-{attempt}\t{pipe}"


class Fixture(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / "dispatch"
        self.root.mkdir()
        self.jobs = self.root / "jobs.log"
        # Route closure age and owed deliveries, by route id / attempt id.
        self.closed_days = {"rt-00000000000000a1": 20, "rt-00000000000000b2": 20,
                            "rt-00000000000000d4": 20, "rt-00000000000000e5": 20,
                            "rt-00000000000000f6": 5}
        self.delivery_owed = {"att-e1"}
        closure = mock.patch.object(A, "_route_closure", side_effect=self._closure)
        delivery = mock.patch.object(A, "_delivery_settled", side_effect=self._delivery)
        closure.start(), delivery.start()
        self.addCleanup(closure.stop), self.addCleanup(delivery.stop)

    def _closure(self, jobs, proven):
        seen = {}

        def closed(meta):
            days = self.closed_days.get(meta.get("route_id"))
            seen[meta["route_id"] + ":k"] = None if days is None else NOW - days * DAY
            return seen[meta["route_id"] + ":k"]
        return closed, seen

    def _delivery(self, root):
        return lambda meta: meta.get("attempt_id") not in self.delivery_owed

    def write(self, lines):
        self.jobs.write_text("".join(line + "\n" for line in lines), encoding="utf-8")

    def live(self):
        return self.jobs.read_text(encoding="utf-8").split("\n")[:-1]

    def archived(self):
        raws = []
        for segment in sorted((self.root / "archive").glob(A.SEGMENT_PREFIX + "*")):
            raws += [json.loads(line)["raw"] for line in (segment / "rows.jsonl").read_text().splitlines()]
        return raws


class SelectionAndLosslessTest(Fixture):
    def setUp(self):
        super().setUp()
        self.closed_days["rt-00000000000000a7"] = 20
        self.log = self.root / "logs" / "att-g1.log"
        self.log.parent.mkdir()
        self.log.write_text("worker log\n")

    def registry(self):
        return {
            # A: a settled owner and its child -> moves together
            "a_owner": row("att-a1", route="rt-00000000000000a1", worker_type="owner"),
            "a_child": row("att-a2", route="rt-00000000000000a1", parent_attempt_id="att-a1"),
            # B: same closed age, but one row ended three days ago -> the whole route stays
            "b_old": row("att-b1", route="rt-00000000000000b2"),
            "b_recent": row("att-b2", days_ago=3, route="rt-00000000000000b2"),
            # C: route still open (no closure) -> stays
            "c_open_route": row("att-c1", route="rt-00000000000000c3"),
            # D: settled itself, but a live row names it -> stays with that row
            "d_settled": row("att-d1", route="rt-00000000000000d4"),
            "d_live_child": row("att-d2", days_ago=0, status="running", route="rt-00000000000000zz",
                                parent_attempt_id="att-d1"),
            # E: a parent delivery still owed -> stays
            "e_owed": row("att-e1", route="rt-00000000000000e5", delivery_intent="1"),
            # F: route closed only five days ago -> stays
            "f_recent_close": row("att-f1", route="rt-00000000000000f6"),
            # G: its attempt log is not retired yet (log retention still needs the row) -> stays
            "g_log_left": row("att-g1", route="rt-00000000000000a7", log_file=str(self.log)),
            "malformed": "not\ta\trow",
        }

    def test_only_whole_settled_groups_move_and_every_byte_is_kept(self):
        rows = self.registry()
        original = list(rows.values()) + [rows["c_open_route"]]   # a duplicate line stays too
        self.write(original)
        result = A.sweep(self.jobs, now_ns=NOW, force=True)
        self.assertEqual(result["removed"], 2, result)
        self.assertEqual(sorted(self.archived()), sorted([rows["a_owner"], rows["a_child"]]))
        self.assertEqual(self.live(), [line for line in original if line not in (rows["a_owner"], rows["a_child"])])
        self.assertEqual(Counter(original), Counter(self.live()) + Counter(self.archived()))

    def test_the_registry_inventory_reads_the_moved_rows_as_history(self):
        rows = self.registry()
        self.write(list(rows.values()))
        A.sweep(self.jobs, now_ns=NOW, force=True)
        history = I.inventory_query(self.root, include_archive=True).rows
        self.assertEqual(sorted(item["raw"] for item in history), sorted([rows["a_owner"], rows["a_child"]]))
        self.assertTrue(all(item["source"] == "archive" for item in history))
        self.assertEqual(A.archived_rows(self.jobs, "rt-00000000000000a1"), [rows["a_owner"], rows["a_child"]])
        self.assertEqual(A.archived_rows(self.jobs, "att-a2"), [rows["a_child"]])

    def test_a_closed_route_replay_finds_its_archived_rows(self):
        import work_start as W
        rows = self.registry()
        self.write(list(rows.values()))
        A.sweep(self.jobs, now_ns=NOW, force=True)
        self.assertNotIn("att-a1", W._rows(self.jobs))
        found = W._archived_rows(self.jobs, "rt-00000000000000a1")
        self.assertEqual(set(found), {"att-a1", "att-a2"})
        self.assertEqual(found["att-a1"][0], "done")
        self.assertEqual(found["att-a1"][1]["worker_type"], "owner")

    def test_a_pass_runs_once_per_due_window(self):
        self.write(list(self.registry().values()))
        A.sweep(self.jobs, now_ns=NOW, force=True)
        self.assertEqual(A.sweep(self.jobs, now_ns=NOW + 60 * 1_000_000_000), {"skipped": "not-due"})


class RaceAndCrashTest(Fixture):
    def test_a_row_written_during_the_pass_keeps_the_group_it_names(self):
        settled = [row("att-a1"), row("att-a2", parent_attempt_id="att-a1")]
        other = row("att-b1", route="rt-00000000000000b2")
        self.write(settled + [other])
        later = row("att-n1", days_ago=0, status="open", route="rt-00000000000000nn", parent_attempt_id="att-a2")
        unrelated = row("att-n2", days_ago=0, status="open", route="rt-00000000000000mm")
        real_archive = A._archive

        def archive_then_register(*args, **kwargs):
            written = real_archive(*args, **kwargs)
            with self.jobs.open("a", encoding="utf-8") as handle:   # registrations racing the pass
                handle.write(later + "\n" + unrelated + "\n")
            return written
        with mock.patch.object(A, "_archive", side_effect=archive_then_register):
            result = A.sweep(self.jobs, now_ns=NOW, force=True)
        self.assertEqual(result["removed"], 1, result)   # only the group no new row names
        self.assertEqual(self.live(), settled + [later, unrelated])
        self.assertEqual(sorted(self.archived()), sorted(settled + [other]))   # a copy, never a loss

    def test_a_crash_before_the_rewrite_loses_nothing_and_never_archives_twice(self):
        settled = [row("att-a1"), row("att-a2", parent_attempt_id="att-a1")]
        keep = row("att-c1", route="rt-00000000000000c3")
        self.write(settled + [keep])
        with mock.patch.object(A, "_remove", side_effect=OSError("crash")):
            with self.assertRaises(OSError):
                A.sweep(self.jobs, now_ns=NOW, force=True)
            with mock.patch.object(A.time, "time_ns", return_value=NOW + DAY):
                self.assertIsNone(A.sweep_best_effort(self.jobs))   # the reap-watch tail never sees it
        self.assertEqual(self.live(), settled + [keep])
        self.assertEqual(sorted(self.archived()), sorted(settled))
        A.sweep(self.jobs, now_ns=NOW, force=True)
        self.assertEqual(self.live(), [keep])
        self.assertEqual(sorted(self.archived()), sorted(settled))

    def test_the_live_rewrite_only_runs_under_the_registry_lock(self):
        settled = [row("att-a1")]
        self.write(settled)
        with open(f"{self.jobs}.lock", "a") as held:
            fcntl.flock(held.fileno(), fcntl.LOCK_EX)
            with mock.patch.object(A, "LOCK_WAIT_SECONDS", 0.2):
                result = A.sweep(self.jobs, now_ns=NOW, force=True)
        self.assertEqual(result.get("skipped"), "registry-busy", result)
        self.assertEqual(self.live(), settled)
        self.assertEqual(A.sweep(self.jobs, now_ns=NOW, force=True)["removed"], 1)

    def test_a_registry_that_is_not_whole_utf8_rows_is_left_alone(self):
        self.jobs.write_bytes(row("att-a1").encode() + b"\n" + row("att-a2").encode() + b"\xff\n")
        before = self.jobs.read_bytes()
        self.assertEqual(A.sweep(self.jobs, now_ns=NOW, force=True), {"skipped": "registry-not-whole-utf8"})
        self.assertEqual(self.jobs.read_bytes(), before)
        self.jobs.write_bytes(row("att-a1").encode() + b"\n" + row("att-a2").encode())   # torn last row
        self.assertEqual(A.sweep(self.jobs, now_ns=NOW, force=True), {"skipped": "registry-not-whole-utf8"})


class ReaderCompatibilityTest(Fixture):
    def test_registry_readers_see_exactly_the_kept_rows_after_the_rewrite(self):
        import dispatch_registry_cache as C
        from dispatch_contract import parse_registry_metadata
        keep = [row("att-c1", route="rt-00000000000000c3"), row("att-c2", days_ago=0, status="open",
                                                               route="rt-00000000000000c3")]
        self.write([row("att-a1"), row("att-a2", parent_attempt_id="att-a1")] + keep)
        before = C.registry_view(self.jobs)
        self.assertEqual(len(before.lines), 4)
        A.sweep(self.jobs, now_ns=NOW, force=True)
        after = C.registry_view(self.jobs)   # the same process's cached view notices the rewrite
        self.assertEqual(list(after.lines), keep)
        # A plain reader (what an older release does) parses the same rows.
        plain = [parse_registry_metadata(line.split("\t")[5])["attempt_id"]
                 for line in self.jobs.read_text().splitlines()]
        self.assertEqual(plain, ["att-c1", "att-c2"])
        self.assertEqual(os.stat(self.jobs).st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
