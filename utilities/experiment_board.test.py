#!/usr/bin/env python3
"""Tests for `experiment_board.py`: the one summary the shell, Fleet, card, and steward read.

Fixtures live in a `TemporaryDirectory` with their own HOME and without `AGENT_ARTIFACT_ROOT`; no
real artifact root is read.  Run records come from the real `resource_progress` writer through the
catalog test's helpers, so a change to their shape breaks these tests rather than a screen.
"""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])

import importlib.util
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import experiment_board as B  # noqa: E402
import experiment_catalog as E  # noqa: E402

_spec = importlib.util.spec_from_file_location("experiment_catalog_test_base", HERE / "experiment_catalog.test.py")
T = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(T)

NOW = T.NOW
GROUP = "AMI 가변 화자 개선"
HEALTHY = {"state": "ok", "seconds_since_anchor": 60.0}


def condition(name, run_id, group=GROUP):
    return {"display_name": name, "group": group, "refs": {"runs": [{"run_id": run_id}]}}


def synthetic(rows, *, metric=None, unlinked=(), catalog="ok", warnings=()):
    """A minimal `experiment-status/v1` document for the pure `board()` function."""
    conditions = []
    for index, row in enumerate(rows):
        base = {"id": f"cnd_{index:012x}", "display_name": f"조건 {index}", "anomalies": [], "unknown": {},
                "current_run": {"run_id": f"r{index}"}, "run_status": "running", "latest": None, "best": None,
                "comparison": {"state": "no-data", "reason": "no-observation", "baseline": None, "delta": None,
                               "delta_unit": None, "relative_percent": None, "better": None},
                "observed_at": "2026-11-13T00:00:00Z", "other_runs": {"count": 0, "run_ids": []}}
        base.update(row)
        conditions.append(base)
    group = {"id": "grp_000000000001", "display_name": "묶음", "goal": "목표", "conditions": conditions,
             "metric": metric or {"key": "si_sdr", "unit": "dB", "direction": "max", "split": "validation",
                                  "scoring": "s"}, "target": {"kind": "fixed", "value": 8.0}}
    return {"schema": "experiment-status/v1", "generated_at": "2026-11-13T00:00:00Z", "groups": [group],
            "unlinked_runs": {"run_ids": list(unlinked), "truncated": 0},
            "catalog": {"status": catalog, "path": E.CATALOG_REL}, "warnings": list(warnings)}


def ok(value, delta, epoch=5):
    return {"latest": {"key": "si_sdr", "epoch": epoch, "value": value, "nonfinite": False},
            "comparison": {"state": "ok", "reason": None, "baseline": {"value": 8.0, "epoch": None, "label": "fix"},
                           "delta": delta, "delta_unit": "dB", "relative_percent": None, "better": delta > 0}}


class Counts(T.Base):
    def test_b1_counts_use_condition_ids_and_failed_wins_over_stagnant(self):
        root = self.make_root()
        names = ["진행 1", "진행 2", "진행 3", "실패", "실패+정체"]
        self.register(root, {"groups": [T.group_spec()], "conditions": [
            condition(name, f"run-{i}") for i, name in enumerate(names)]})
        for i in range(3):
            self.write_run(root, f"run-{i}", stagnation=HEALTHY)
        self.write_run(root, "run-3", status="failed", stagnation=HEALTHY)
        self.write_run(root, "run-4", status="failed", stagnation={"state": "stagnant", "seconds_since_anchor": 9000.0})
        result = B.board(E.status(root, now=NOW))
        group = result["groups"][0]
        self.assertEqual(group["count"], 5)
        self.assertEqual(group["counts"], {"running": 3, "failed": 2, "stagnant": 0, "unknown": 0, "waiting": 0})
        self.assertEqual(result["strip"], "!2LB")
        self.assertEqual([r["status"] for r in group["rows"]], ["running"] * 3 + ["failed", "failed"])


class Headline(unittest.TestCase):
    def head(self, rows, **kw):
        return B.board(synthetic(rows, **kw))["groups"][0]["headline"]

    def test_b2_priority_failed_then_stagnant_then_unknown_then_not_comparable_then_best(self):
        good = ok(9.1, 1.1)
        failed = {"anomalies": ["status-failed"], "run_status": "failed"}
        stagnant = {"anomalies": ["stagnant"]}
        unknown = {"anomalies": ["record-missing"]}
        incomparable = {"latest": {"key": "si_sdr", "epoch": 5, "value": 5.0, "nonfinite": False},
                        "comparison": {"state": "not-comparable", "reason": "scoring-mismatch", "baseline": None,
                                       "delta": None, "delta_unit": None, "relative_percent": None, "better": None}}
        waiting = {}
        # Every lower-priority row is present in each case, so only the priority decides.
        self.assertEqual(self.head([dict(good), stagnant, unknown, incomparable, waiting, failed])["kind"], "failed")
        self.assertEqual(self.head([dict(good), unknown, incomparable, waiting, stagnant])["kind"], "stagnant")
        self.assertEqual(self.head([dict(good), incomparable, waiting, unknown])["kind"], "unknown")
        self.assertEqual(self.head([dict(good), waiting, incomparable])["kind"], "not-comparable")
        self.assertEqual(self.head([dict(good), waiting])["kind"], "waiting")
        found = self.head([ok(8.4, 0.4), ok(9.1, 1.1), ok(8.8, 0.8)])
        self.assertEqual((found["kind"], found["name"]), ("best", "조건 1"))  # max: the highest latest value
        low = {"key": "wer", "unit": "%", "direction": "min", "split": "validation", "scoring": "s"}
        found = self.head([ok(21.0, 1.0), ok(19.0, -1.0), ok(20.0, 0.0)], metric=low)
        self.assertEqual(found["name"], "조건 1")  # min: the lowest
        tie = self.head([ok(9.0, 1.0), ok(9.0, 1.0)])
        self.assertEqual(tie["name"], "조건 0")  # equal values keep the catalog order

    def test_b3_other_group_attention_does_not_change_the_selected_group(self):
        doc = synthetic([ok(9.0, 1.0)])
        second = dict(doc["groups"][0], id="grp_000000000002", display_name="둘째 묶음",
                      conditions=[dict(doc["groups"][0]["conditions"][0], id="cnd_0000000000ff",
                                       anomalies=["status-failed"], run_status="failed")])
        doc["groups"].append(second)
        result = B.board(doc)
        self.assertEqual(result["selected"], "grp_000000000001")
        self.assertEqual(result["groups"][0]["headline"]["kind"], "best")
        self.assertEqual(result["other_groups_attention"],
                         [{"group_id": "grp_000000000002", "name": "둘째 묶음", "failed": 1, "stagnant": 0, "unknown": 0}])
        self.assertEqual(result["strip"], "!1LB")  # attention anywhere reaches the strip


class Visibility(T.Base):
    def test_b4_hidden_unconfirmed_and_broken_catalogs_are_not_shown_as_no_experiments(self):
        empty = B.board(synthetic([]) | {"groups": []})
        self.assertFalse(empty["visible"])
        self.assertIsNone(empty["strip"])
        # catalog missing + two public records: "?2LB", visible
        root = self.make_root()
        self.write_run(root, "run-x")
        self.write_run(root, "run-y")
        missing = B.board(E.status(root, now=NOW))
        self.assertTrue(missing["visible"])
        self.assertEqual((missing["strip"], missing["unlinked"]["count"]), ("?2LB", 2))
        # a broken catalog is unconfirmed, not zero experiments
        broken = self.make_root("broken", "b")
        path = E.catalog_path(broken)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json", encoding="utf-8")
        result = B.board(E.status(broken, now=NOW))
        self.assertTrue(result["visible"])
        self.assertEqual(result["catalog"]["status"], "invalid")
        self.assertEqual(result["strip"], "?1LB")
        # no catalog and no run at all: a new project, nothing to say
        blank = B.board(E.status(self.make_root("blank", "c"), now=NOW))
        self.assertFalse(blank["visible"])

    def test_b5_strip_tokens(self):
        group = {"count": 5}
        zero = {"running": 0, "failed": 0, "stagnant": 0, "unknown": 0, "waiting": 0}
        self.assertEqual(B._strip(group, zero, 0), "5 LB")
        self.assertEqual(B._strip(group, dict(zero, failed=1), 0), "!1LB")
        self.assertEqual(B._strip(group, dict(zero, unknown=1), 2), "?3LB")
        self.assertEqual(B._strip({"count": 12}, zero, 0), "9+LB")
        self.assertEqual(B._strip(group, dict(zero, failed=1, unknown=2), 1), "!1LB")  # "!" outranks "?"
        self.assertEqual(B._strip(group, dict(zero, stagnant=11), 0), "!9+LB")


class Values(unittest.TestCase):
    def test_b6_not_comparable_carries_no_delta_and_no_experiment_unit_on_the_baseline(self):
        row = {"latest": {"key": "si_sdr", "epoch": 5, "value": 9.0, "nonfinite": False},
               "comparison": {"state": "not-comparable", "reason": "unit-mismatch", "baseline": {"value": 3.0, "epoch": 4, "label": "기준"},
                              "delta": None, "delta_unit": None, "relative_percent": None, "better": None}}
        got = B.board(synthetic([row]))["groups"][0]["rows"][0]
        self.assertIsNone(got["delta"])
        self.assertIsNone(got["baseline_unit"])  # 3.0 was not measured in dB
        self.assertEqual((got["baseline"]["value"], got["reason"]), (3.0, "unit-mismatch"))
        valid = B.board(synthetic([ok(9.1, 1.1)]))["groups"][0]["rows"][0]
        self.assertEqual((valid["delta"], valid["baseline_unit"], valid["delta_unit"]), (1.1, "dB", "dB"))

    def test_b10_rows_keep_catalog_order_when_values_change(self):
        first = B.board(synthetic([ok(9.1, 1.1), ok(8.0, 0.0), ok(8.8, 0.8)]))["groups"][0]
        second = B.board(synthetic([ok(7.0, -1.0), ok(9.9, 1.9), ok(8.8, 0.8)]))["groups"][0]
        self.assertEqual([r["name"] for r in first["rows"]], [r["name"] for r in second["rows"]])
        self.assertEqual([r["name"] for r in second["rows"]], ["조건 0", "조건 1", "조건 2"])


class ReadCache(T.Base):
    def setup_root(self, **run):
        root = self.make_root()
        self.register(root, {"groups": [T.group_spec()], "conditions": [condition("짧은 발화 유지", "run-a")]})
        self.write_run(root, "run-a", stagnation=HEALTHY, **run)
        return root

    def test_b7_rename_shows_on_the_next_read_even_inside_the_ttl(self):
        root = self.setup_root()
        cache = {}
        first, _ = B.read(root, now=NOW, cache=cache)
        self.assertEqual(first["groups"][0]["rows"][0]["name"], "짧은 발화 유지")
        E.run_write(root, lambda ctx: E.op_rename(ctx, "짧은 발화 유지", "아주 길게 바꾼 새 이름"), now=NOW + 1)
        second, watch = B.read(root, now=NOW + 2, cache=cache)  # 2 s later: inside the TTL
        self.assertEqual(second["groups"][0]["rows"][0]["name"], "아주 길게 바꾼 새 이름")
        self.assertIn(str(E.catalog_path(root)), watch)

    def test_b7c_a_rewrite_that_restores_size_and_mtime_is_still_seen(self):
        # The same defect `route.py` fixed in #1595: a key of (mtime, size) misses an in-place edit
        # whose length and mtime were put back, so the screen would keep the old name for a TTL.
        root = self.setup_root()
        cache = {}
        first, _ = B.read(root, now=NOW, cache=cache)
        self.assertEqual(first["groups"][0]["rows"][0]["name"], "짧은 발화 유지")
        path = E.catalog_path(root)
        before = os.stat(path)
        data = path.read_bytes()
        edited = data.replace("짧은 발화 유지".encode(), "짧은 발화 보존".encode())
        self.assertEqual(len(edited), len(data))
        self.assertNotEqual(edited, data)
        with open(path, "r+b") as handle:      # in place: same inode, same size
            handle.write(edited)
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))  # and the same mtime
        after = os.stat(path)
        self.assertEqual((after.st_ino, after.st_size, after.st_mtime_ns), (before.st_ino, before.st_size, before.st_mtime_ns))
        second, _ = B.read(root, now=NOW + 2, cache=cache)  # inside the TTL
        self.assertEqual(second["groups"][0]["rows"][0]["name"], "짧은 발화 보존")

    def test_b7d_a_change_while_status_runs_is_read_again_inside_the_ttl(self):
        # The key is the stat before the read: a file that moves while `status()` runs must not be
        # filed under its new stat with the old board (a stale name for a whole TTL, #1595's kind).
        root = self.setup_root()
        cache = {}
        real = E.status
        def renaming(*args, **kwargs):
            doc = real(*args, **kwargs)           # the read ends with the old name...
            E.run_write(root, lambda ctx: E.op_rename(ctx, "짧은 발화 유지", "읽는 동안 바뀐 이름"), now=NOW + 1)
            return doc                            # ...and the catalog changes before it is filed
        with mock.patch.object(E, "status", side_effect=renaming):
            first, _ = B.read(root, now=NOW, cache=cache)
        self.assertEqual(first["groups"][0]["rows"][0]["name"], "짧은 발화 유지")
        second, _ = B.read(root, now=NOW + 2, cache=cache)        # inside the TTL
        self.assertEqual(second["groups"][0]["rows"][0]["name"], "읽는 동안 바뀐 이름")

    def test_b7e_a_record_that_changes_while_status_runs_is_read_again_inside_the_ttl(self):
        root = self.setup_root(reached=12)
        cache = {}
        B.read(root, now=NOW, cache=cache)                         # the record is in the watch list now
        real = E.status
        def advancing(*args, **kwargs):
            doc = real(*args, **kwargs)
            self.write_run(root, "run-a", stagnation=HEALTHY, reached=30)   # the run reports while we read
            return doc
        with mock.patch.object(E, "status", side_effect=advancing):
            old, _ = B.read(root, now=NOW + B.TTL_SECONDS + 1, cache=cache)   # TTL over: read again
        self.assertEqual(old["groups"][0]["rows"][0]["progress"]["reached"], 12)
        fresh, _ = B.read(root, now=NOW + B.TTL_SECONDS + 2, cache=cache)     # inside the new TTL
        self.assertEqual(fresh["groups"][0]["rows"][0]["progress"]["reached"], 30)

    def test_b11_the_last_progress_moment_is_kept_so_a_screen_counts_its_own_age(self):
        root = self.setup_root()
        stagnant = {"state": "ok", "seconds_since_anchor": 600.0}
        self.write_run(root, "run-a", stagnation=stagnant)
        row = B.read(root, now=NOW, cache={})[0]["groups"][0]["rows"][0]
        self.assertEqual(row["since_last_progress_seconds"], 600)
        self.assertEqual(B.age_seconds(row, NOW, "last_progress_at"), 600)
        self.assertEqual(B.age_seconds(row, NOW + 120, "last_progress_at"), 720)
        self.assertEqual(B.age_seconds(row, NOW - 630, "last_progress_at"), 0)     # inside the minute, not unknown
        self.assertIsNone(B.age_seconds(row, NOW - 1000, "last_progress_at"))      # a clock that disagrees

    def test_b7b_an_unchanged_redraw_reads_the_status_once(self):
        root = self.setup_root()
        cache = {}
        with mock.patch.object(E, "status", wraps=E.status) as status:
            for step in range(5):
                B.read(root, now=NOW + step, cache=cache)
            self.assertEqual(status.call_count, 1)  # the redraws were `stat` calls only
            B.read(root, now=NOW + B.TTL_SECONDS + 1, cache=cache)
            self.assertEqual(status.call_count, 2)

    def test_b8_unchanged_record_becomes_stale_within_a_minute_of_the_mark(self):
        root = self.setup_root(at=NOW - 850)
        cache = {}
        before, _ = B.read(root, now=NOW, cache=cache)
        self.assertNotIn("observation-stale", before["groups"][0]["rows"][0]["anomalies"])
        inside, _ = B.read(root, now=NOW + 30, cache=cache)  # cached: still the old verdict, at most a TTL late
        self.assertIs(inside, before)
        later, _ = B.read(root, now=NOW + 61, cache=cache)
        row = later["groups"][0]["rows"][0]
        self.assertIn("observation-stale", row["anomalies"])
        self.assertEqual(row["status"], "unknown")
        self.assertEqual(B.age_seconds(row, NOW + 61), 911)  # the age is the render time's, not the cached one
        self.assertEqual(B.age_seconds(before["groups"][0]["rows"][0], NOW + 61), 911)
        self.assertEqual(later["strip"], "?1LB")

    def test_b9_a_failed_read_is_an_unconfirmed_board_not_an_empty_one(self):
        root = self.setup_root()
        with mock.patch.object(E, "status", side_effect=RuntimeError("boom")):
            result, watch = B.read(root, now=NOW, cache={})
        self.assertTrue(result["visible"])
        self.assertEqual((result["catalog"]["status"], result["strip"]), ("unreadable", "?1LB"))
        self.assertTrue(watch)  # a later change to the catalog still wakes the screen


class NoLeftovers(T.Base):
    def test_zz_no_child_process_is_left(self):
        with self.assertRaises(ChildProcessError):
            os.waitpid(-1, os.WNOHANG)


if __name__ == "__main__":
    unittest.main()
