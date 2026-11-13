"""The selection index's per-row facts are kept per row tuple; a pass still answers as one built from nothing."""
import os
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "utilities"))
from fleet.collectors import dispatch
from fleet.collectors import procscan

NOW = "2026-11-12T12:00:00Z"


def row(index, status, **meta):
    base = {"attempt_id": "att-%d" % index, "attempt_schema_version": "2", "dispatch_depth": "1",
            "worker_type": "owner"}
    base.update(meta)
    text = ",".join("%s=%s" % (key, value) for key, value in base.items())
    return "\t".join(("2026-11-12T%02d:%02d:00Z" % (index // 60 % 24, index % 60), status, "/repo",
                      "/wt" if status in ("open", "running") else "-", "slug-%d" % index, text))


def registry_rows():
    rows = []
    for index in range(30):
        if index % 7 == 0:
            rows.append(row(index, "open", pid=str(os.getpid()), pid_start="1"))
        elif index % 5 == 0:
            rows.append(row(index, "done", note="dead-worker", pid="999999", pid_start="7"))
        elif index % 3 == 0:
            rows.append(row(index, "done", intensity="quick", note="dead-quick"))
        else:
            rows.append(row(index, "done"))
    rows.append("2026-11-12T01:00:00Z\tkilled\t/repo\t-\tno-attempt\tnote=legacy")
    return rows


class JobSelectionFactsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.jobs = Path(self.temp.name) / "jobs.log"
        self.rows = registry_rows()
        self.write()
        for table in (dispatch._JOB_SELECTION_CACHE, ):
            table.clear()
        dispatch._SELECTION_FACTS.update(scope=None, rows={})
        self.addCleanup(dispatch._JOB_SELECTION_CACHE.clear)
        if procscan._visible_pids() is None:
            self.skipTest("no /proc view: the compiled selection is not used here")

    def write(self):
        self.jobs.write_text("\n".join(self.rows) + "\n", encoding="utf-8")

    def select(self, include_stall=True):
        with mock.patch.object(dispatch._dispatch_registry_cache, "_PARSE_CHUNK_LINES", 4), \
                dispatch.collection_read_scope(), dispatch._dispatch_registry_cache.registry_read_scope():
            parsed = dispatch.parsed_registry_rows(self.jobs)
            derived = dispatch._registry_derived_index(self.jobs, parsed)
            self.assertIn("token", derived)
            return dispatch._job_selection(derived, {"att-3"}, include_stall=include_stall)

    def fresh(self, include_stall=True):
        """The same answer from a process that kept nothing."""
        dispatch._JOB_SELECTION_CACHE.clear()
        dispatch._SELECTION_FACTS.update(scope=None, rows={})
        dispatch._PARSED_REGISTRY_INDEXES.clear()
        dispatch._REGISTRY_INDEX_PARTS.clear()
        return self.select(include_stall)

    def stats(self):
        return dispatch.registry_fold_metrics()

    def assertSame(self, label):
        for include_stall in (True, False):
            warm = self.select(include_stall)
            # fresh() drops every cache this pass relies on, so compare on a copy of the memo state
            kept = (dict(dispatch._SELECTION_FACTS), list(dispatch._JOB_SELECTION_CACHE.items()))
            fresh = self.fresh(include_stall)
            dispatch._SELECTION_FACTS.update(kept[0])
            self.assertEqual(list(warm[0]), list(fresh[0]), label)
            self.assertEqual(warm[1], fresh[1], label)
            self.assertEqual(warm[2], fresh[2], label)
            self.assertEqual(dict(warm[3]), dict(fresh[3]), label)

    def test_unchanged_registry_asks_nothing_again_and_answers_as_fresh(self):
        start = self.stats()
        first = self.select()
        built = self.stats()["selection_fact_builds"]
        self.assertEqual(built - start["selection_fact_builds"], len(self.rows))   # every job's facts, once
        dispatch._JOB_SELECTION_CACHE.clear()                         # the compiled index is gone, the facts stay
        again = self.select()
        stats = self.stats()
        self.assertEqual(stats["selection_fact_builds"], built)       # nothing is worked out a second time
        self.assertEqual(stats["selection_fact_reuses"] - start["selection_fact_reuses"], len(self.rows))
        self.assertEqual((list(again[0]), again[1], again[2], dict(again[3])),
                         (list(first[0]), first[1], first[2], dict(first[3])))
        self.assertSame("unchanged")

    def test_a_rewrite_builds_the_facts_of_the_changed_rows_only(self):
        self.select()
        built = self.stats()["selection_fact_builds"]
        index = next(i for i, text in enumerate(self.rows) if "att-12" in text)
        self.rows[index] = self.rows[index].replace("done", "killed", 1) if "\tdone\t" in self.rows[index] \
            else self.rows[index].replace("note=", "note=edited-", 1)
        self.write()
        self.select()
        changed = self.stats()["selection_fact_builds"] - built
        self.assertGreaterEqual(changed, 1)
        self.assertLess(changed, 12)                # the rows of the rebuilt segments, never the whole registry
        self.assertGreater(self.stats()["selection_fact_reuses"], 0)

    def test_every_kind_of_change_still_answers_as_fresh(self):
        self.assertSame("start")
        self.rows.append(row(40, "open", pid=str(os.getpid()), pid_start="1"))      # appended
        self.write()
        self.assertSame("append")
        self.rows[3] = self.rows[3].replace("\tdone\t", "\topen\t", 1)               # a finished row reopens
        self.write()
        self.assertSame("reopened in place")
        self.rows[9] = self.rows[9].replace("slug-9", "slug-9,note=dead-late", 1)    # becomes a dead owner
        self.write()
        self.assertSame("dead owner in place")
        del self.rows[2]                                                              # an early row deleted
        self.write()
        self.assertSame("deleted")

    def test_the_compiled_index_is_charged_by_schema_and_never_sized_by_walking_it(self):
        from runtime_observation import retained_size
        with mock.patch.object(dispatch._JOB_SELECTION_CACHE, "remember",
                               wraps=dispatch._JOB_SELECTION_CACHE.remember) as remember:
            self.select()
        self.assertEqual(remember.call_count, 1)
        self.assertIsNotNone(remember.call_args.kwargs.get("weight"))          # a charge, not a walk of the index
        (key, compiled), = list(dispatch._JOB_SELECTION_CACHE.items())
        charged = dispatch._JOB_SELECTION_CACHE.weights[key]
        self.assertGreaterEqual(charged, retained_size((key, compiled)))       # never below what it holds
        self.assertEqual(charged, dispatch._JOB_SELECTION_BYTES_PER_JOB * len(compiled["positions"]) + 8192)

    def test_another_namespace_or_policy_keeps_nothing(self):
        self.select()
        built = self.stats()["selection_fact_builds"]
        with mock.patch.object(dispatch, "_pid_namespace_identity", return_value="pid:[elsewhere]"):
            self.select()
        self.assertEqual(self.stats()["selection_fact_builds"] - built, len(self.rows))
        built = self.stats()["selection_fact_builds"]
        real = dispatch._dead_terminal_owner_row
        with mock.patch.object(dispatch, "_dead_terminal_owner_row", side_effect=lambda s, m: real(s, m)):
            self.select()
        self.assertEqual(self.stats()["selection_fact_builds"] - built, len(self.rows))


if __name__ == "__main__":
    unittest.main()
