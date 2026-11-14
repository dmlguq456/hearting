#!/usr/bin/env python3
"""Tests for `experiment_board.py`: the one summary the shell, Fleet, card, and steward read.

Fixtures live in a `TemporaryDirectory` with their own HOME and without `AGENT_ARTIFACT_ROOT`; no
real artifact root is read.  Run records come from the real `resource_progress` writer through the
catalog test's helpers, so a change to their shape breaks these tests rather than a screen.
"""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])

import datetime
import importlib.util
import json
import subprocess
import unittest
import uuid
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
        self.write_run(root, "run-a", stagnation=stagnant)   # observed at NOW - 180: progress at NOW - 780
        row = B.read(root, now=NOW, cache={})[0]["groups"][0]["rows"][0]
        self.assertEqual(row["since_last_progress_seconds"], 600)
        self.assertEqual(B.age_seconds(row, NOW, "last_progress_at"), 780)
        self.assertEqual(B.age_seconds(row, NOW + 120, "last_progress_at"), 900)
        self.assertEqual(B.age_seconds(row, NOW - 810, "last_progress_at"), 0)     # inside the minute, not unknown
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


class ObserverWatching(T.Base):
    """BC-LAB-OBSERVATION-STALE-FLICKER-1114: a run whose observer is watching it is not "unknown"
    between two measurements, however long an epoch is; a run nobody can vouch for still is."""

    def setup_root(self):
        root = self.make_root()
        self.register(root, {"groups": [T.group_spec()], "conditions": [condition("긴 에폭", "run-long")]})
        return root

    def look(self, root, now, state, *, alive):
        run = {"run_id": "run-long", "started_at": NOW - 3600, "artifact_root": str(root), "status": "running"}
        with mock.patch.object(B.rp, "_observation_state_path", return_value=state):
            B.rp.observe_experiment(run, root / "registry.json", now, row_key="run", started_at=NOW - 3600,
                                    status="running", notify_events=False, alive=alive)

    def watch(self, alive):
        """Forty minutes of 60 s looks with nothing new measured; the board read at every look."""
        root = self.setup_root()
        state = self.base / f"state-{alive}.json"
        stale_at, writes = [], 0
        real = B.rp.write_public_run_record

        def counted(*args, **kwargs):
            nonlocal writes
            writes += 1
            return real(*args, **kwargs)
        with mock.patch.object(B.rp, "write_public_run_record", side_effect=counted):
            for minute in range(41):
                now = NOW + 60 * minute
                self.look(root, now, state, alive=alive)
                row = B.read(root, now=now, cache={})[0]["groups"][0]["rows"][0]
                if "observation-stale" in row["anomalies"]:
                    stale_at.append(minute)
        return stale_at, writes

    def test_a_watching_observer_keeps_a_long_epoch_observed(self):
        stale_at, writes = self.watch(alive=True)
        self.assertEqual(stale_at, [])
        # The cost: one public record write per PUBLIC_REFRESH_SECONDS while nothing changes.
        self.assertEqual(writes, 1 + 40 * 60 // B.rp.PUBLIC_REFRESH_SECONDS)

    def test_an_observer_that_cannot_vouch_for_the_run_still_reads_stale(self):
        stale_at, writes = self.watch(alive=False)
        self.assertEqual(stale_at[0], 16)            # past the 900 s mark, as before
        self.assertEqual(stale_at, list(range(16, 41)))
        self.assertEqual(writes, 1)

    def test_a_local_run_published_as_working_is_a_running_row(self):
        # A local resource run's observer publishes `working`; the board counts it running and
        # applies the same stale rule to it.
        root = self.setup_root()
        self.write_run(root, "run-long", status="working", at=NOW - 60, stagnation=HEALTHY)
        row = B.read(root, now=NOW, cache={})[0]["groups"][0]["rows"][0]
        self.assertEqual((row["run_status"], row["status"]), ("running", "running"))
        self.write_run(root, "run-long", status="working", at=NOW - 1000, stagnation=HEALTHY)
        row = B.read(root, now=NOW, cache={})[0]["groups"][0]["rows"][0]
        self.assertIn("observation-stale", row["anomalies"])


class StoppedRunsStayVisible(T.Base):
    """RV-PR1619-1114: keeping a watched run "observed" must not hide a run that stopped.

    The observer's own look (observed time) and the run's last valid progress (progress time) are kept
    apart: a live process whose progress file stopped turns stagnant by the existing two-hour rule, a
    declared progress source that can no longer be read is not vouched for, and a compute run whose
    files stopped is vouched for only up to its last own evidence, never again from "now"."""

    def setup_root(self, run_id):
        root = self.make_root()
        self.register(root, {"groups": [T.group_spec()], "conditions": [condition("멈춘 실행", run_id)]})
        return root

    def row(self, root, now):
        return B.read(root, now=now, cache={})[0]["groups"][0]["rows"][0]

    def live_child(self):
        child = subprocess.Popen(["sleep", "600"], start_new_session=True)

        def stop():
            child.terminate()
            child.wait(timeout=5)
        self.addCleanup(stop)
        return child

    def local_run(self, root, progress_file):
        import resource_run_registry as RR
        run_id = "frozen-" + uuid.uuid4().hex
        child = self.live_child()
        run = {"run_id": run_id, "artifact_root": str(root), "started_at": NOW - 600, "status": "running",
               "node": "train", "cwd": str(self.base), "progress_file": str(progress_file)}
        identity = RR.launched_identity(child, attempts=500)    # cmdline can be empty just after exec
        self.assertIsNotNone(identity)
        run.update(identity)
        run["pid_namespace"] = os.readlink("/proc/self/ns/pid")
        return RR, run

    def write_counter(self, path, run_id, completed, updated_at):
        path.write_text(json.dumps({"schema_version": 1, "run_id": run_id, "node": "train",
                                    "updated_at": updated_at, "completed": completed, "total": 60,
                                    "unit": "epoch"}))

    def test_a_live_process_whose_progress_file_stopped_turns_stagnant(self):
        progress_file = self.base / "progress.json"
        root = self.make_root()
        RR, run = self.local_run(root, progress_file)
        self.register(root, {"groups": [T.group_spec()], "conditions": [condition("멈춘 실행", run["run_id"])]})
        self.write_counter(progress_file, run["run_id"], 12, NOW)
        state = self.base / "obs.json"
        seen = {}
        with mock.patch.object(B.rp, "_observation_state_path", return_value=state), \
                mock.patch.dict(os.environ, {"FLEET_NOW_LANG": "Korean"}):
            for sec in range(0, 9001, 60):
                view = RR.observe_run_if_due(run["run_id"], run, root / "registry.json", now=NOW + sec)
                self.assertEqual(view["liveness"], "working")            # the process really is alive
                if sec in (600, 1800, 7200, 9000):
                    observed = B.rp._read_observation_state(state)
                    row = self.row(root, NOW + sec)
                    text = "; ".join(B.rp._notice_texts(run, "checkpoint", observed["observation"], None, []))
                    seen[sec] = (row, observed, text)
        for sec, (row, observed, text) in seen.items():
            with self.subTest(seconds=sec):
                # The progress moment is the file's, kept across every refresh of the observed time.
                self.assertEqual(B.age_seconds(row, NOW + sec, "last_progress_at"), sec)
                self.assertEqual(observed["progress_tracker"]["first_observed_at"], NOW)
                self.assertIn("진행 12/60 epoch", text)
                self.assertNotIn("정체 여부=미확인", text)
                self.assertNotIn("observation-stale", row["anomalies"])   # the observer is watching it
        self.assertEqual(seen[600][0]["status"], "running")
        self.assertEqual(seen[1800][0]["status"], "running")              # inside the two-hour window
        self.assertIn("마지막 갱신 30m 전", seen[1800][2])
        for sec in (7200, 9000):
            row, observed, _text = seen[sec]
            self.assertEqual(row["status"], "stagnant")
            self.assertIn("stagnant", row["anomalies"])
            notices = [item for item in observed["outbox"] if item["kind"] == "attention"]
            self.assertEqual(len(notices), 1)                              # said once, still standing at 9000 s
            self.assertIn("2h00m 동안 오름 없음", notices[0]["text"])          # the notice's words in the declared language

    def test_a_declared_progress_source_that_can_no_longer_be_read_is_not_vouched_for(self):
        progress_file = self.base / "progress.json"
        root = self.make_root()
        RR, run = self.local_run(root, progress_file)
        self.register(root, {"groups": [T.group_spec()], "conditions": [condition("멈춘 실행", run["run_id"])]})
        state = self.base / "obs.json"
        statuses = {}
        with mock.patch.object(B.rp, "_observation_state_path", return_value=state):
            for minute in range(41):
                now = NOW + 60 * minute
                if minute <= 10:
                    self.write_counter(progress_file, run["run_id"], 12 + minute, now)
                elif progress_file.exists():
                    progress_file.unlink()                               # the counter is gone at minute 11
                RR.observe_run_if_due(run["run_id"], run, root / "registry.json", now=now)
                statuses[minute] = self.row(root, now)["status"]
        self.assertEqual({statuses[m] for m in range(0, 11)}, {"running"})
        # The last record was written when the counter went missing (minute 11); nobody vouches after it.
        self.assertEqual(statuses[26], "running")
        self.assertEqual({statuses[m] for m in range(27, 41)}, {"unknown"})

    # -- compute runs: the watcher's evidence is the run's own files on the shared run root -------------

    def compute(self):
        spec = importlib.util.spec_from_file_location("compute_hosts_for_board", HERE / "compute-hosts.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def compute_timeline(self, kind, minutes=151):
        """One synthetic compute run looked at every 60 s; the board's status at every minute."""
        module = self.compute()
        run_id = "remote-" + uuid.uuid4().hex
        root = self.setup_root(run_id)
        run_dir = self.base / run_id
        run_dir.mkdir()
        meta = {"run_id": run_id, "host": "gpu-x", "artifact_root": str(root),
                "started_at": datetime.datetime.fromtimestamp(NOW - 600, datetime.timezone.utc).isoformat()}
        (run_dir / "meta.json").write_text(json.dumps(meta))
        os.utime(run_dir / "meta.json", (NOW - 600, NOW - 600))
        log = run_dir / "log"
        log.write_text("epoch 1\n")
        os.utime(log, (NOW, NOW))
        state = self.base / f"{kind}.json"
        writes, statuses = [], []
        real = B.rp.write_public_run_record

        def counted(*args, **kwargs):
            writes.append(current[0])
            return real(*args, **kwargs)
        current = [0]
        with mock.patch.object(B.rp, "_observation_state_path", return_value=state), \
                mock.patch.object(B.rp, "write_public_run_record", side_effect=counted):
            for minute in range(minutes):
                current[0] = minute
                now = NOW + 60 * minute
                if kind == "healthy":
                    os.utime(log, (now - 5, now - 5))                    # the run keeps writing its log
                if kind == "metadata-only":
                    os.utime(run_dir / "meta.json", (now, now))         # Hearting rewrites meta.json; the run is silent
                if kind != "observer-stopped" or minute == 0:
                    module._observe_compute_experiment(meta, run_dir, now)
                statuses.append(self.row(root, now)["status"])
        return statuses, writes

    def test_a_compute_run_whose_files_stopped_turns_unknown_when_it_did_before(self):
        statuses, writes = self.compute_timeline("remote-stopped", minutes=40)
        self.assertEqual(statuses.index("unknown"), 16)                   # base: 16 min; not 31
        self.assertEqual(set(statuses[16:]), {"unknown"})
        self.assertEqual(writes, [0])                                     # no refresh vouched past t=0

    def test_a_compute_run_whose_observer_stopped_still_turns_unknown_at_sixteen_minutes(self):
        statuses, _writes = self.compute_timeline("observer-stopped", minutes=40)
        self.assertEqual(statuses.index("unknown"), 16)

    def test_a_compute_run_with_only_its_metadata_changing_is_not_vouched_for(self):
        statuses, _writes = self.compute_timeline("metadata-only", minutes=40)
        self.assertEqual(statuses.index("unknown"), 16)

    def test_a_healthy_compute_run_writing_its_log_never_reads_unknown(self):
        statuses, writes = self.compute_timeline("healthy", minutes=121)
        self.assertEqual(set(statuses), {"running"})
        self.assertLessEqual(len(writes), 1 + 120 * 60 // B.rp.PUBLIC_REFRESH_SECONDS)


class Review(T.Base):
    """RV-PR1606-1113: the progress age, the best value's metric, and long numbers."""

    def fixture(self):
        root = self.make_root()
        self.register(root, {"groups": [T.group_spec()], "conditions": [condition("조건 0", "r0")]})
        return root

    def test_progress_age(self):
        """`seconds_since_anchor` was measured at the observation, not at this read: an hour-old record that said
        "two hours since progress" means three hours now, and the age grows on every later read."""
        root = self.fixture()
        self.write_run(root, "r0", at=NOW - 3600, stagnation={"state": "stagnant", "seconds_since_anchor": 7200})
        cache, ages = {}, []
        for now in (NOW, NOW + 61, NOW + 122):
            row = B.read(root, now=now, cache=cache)[0]["groups"][0]["rows"][0]
            ages.append(B.age_seconds(row, now, "last_progress_at"))
        self.assertEqual(ages, [10800, 10861, 10922])
        self.assertEqual(row["last_progress_at"], E._rfc3339(NOW - 10800))   # the same moment whenever it is read

    def test_best_metric_identity(self):
        """A WER (%) group whose run reported only loss: the best is loss's best, and it says so."""
        root = self.fixture()
        self.write_run(root, "r0", key="loss", series={"loss": [(1, 0.5), (2, 0.7)]}, stagnation=HEALTHY)
        group = B.read(root, now=NOW, cache={})[0]["groups"][0]
        row = group["rows"][0]
        self.assertEqual((row["latest"]["key"], row["best"]["key"]), ("loss", "loss"))
        self.assertEqual(B.latest_text(row, group["metric"], unit=True), "0.7 (loss)")
        self.assertEqual(B.best_text(row, group["metric"], unit=True), "0.5 (loss)")   # not "0.5%@1"


class NoLeftovers(T.Base):
    def test_zz_no_child_process_is_left(self):
        with self.assertRaises(ChildProcessError):
            os.waitpid(-1, os.WNOHANG)


if __name__ == "__main__":
    unittest.main()
