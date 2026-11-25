#!/usr/bin/env python3
"""A main session's work-style record gets its field and unit labels by itself (D-82, 12b).

Every case runs the real ``mem add`` against a throwaway store, session ledger and
registry; nothing reads or writes a live store.
"""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../../run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])

import json
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[3]
MEM = ROOT / "tools" / "memory" / "mem.py"
sys.path.insert(0, str(ROOT / "utilities"))
from harness_capabilities import session_env  # noqa: E402
from dispatch_contract import supervisor_lease_path  # noqa: E402
import turn_change_record as T  # noqa: E402

HARNESSES = ("claude", "codex", "opencode")
SESSION = "sess-labels-1"
BODY = "Put blocking findings first in every review."


class WorkLabelsTest(unittest.TestCase):
    harness = "claude"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="mem-labels-", dir="/tmp")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for name in ("home", "project", "routes", "dispatch"):
            (self.root / name).mkdir()
        self.jobs = self.root / "dispatch" / "jobs.log"
        self.n = 0

    def env(self, **extra):
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("MEM_", "AGENT_", "CODEX_", "OPENCODE_", "CLAUDE", "FLEET_"))}
        env.pop("PYTHONPYCACHEPREFIX", None)
        env.update({"HOME": str(self.root / "home"), "XDG_STATE_HOME": str(self.root / "state"),
                    "MEM_STORE": str(self.root / "store"), "MEM_EXCHANGE_AUTO": "0", "TMPDIR": "/tmp",
                    "MEM_RECALL_EVENTS": str(self.root / "events.jsonl"),
                    "FLEET_ROUTE_CHAIN_DIR": str(self.root / "chains"),
                    "AGENT_DISPATCH_JOBS": str(self.jobs),
                    "AGENT_DISPATCH_CALLER_HARNESS": self.harness,
                    session_env()[self.harness][0]: SESSION})
        env.update(extra)
        return env

    def mem(self, *args, **env):
        done = subprocess.run([sys.executable, str(MEM), *args], cwd=self.root / "project", env=self.env(**env),
                              text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        return done

    def route(self, field="code", capability="autopilot-code", ts=None, ended=None, source=None, session=SESSION,
              automatic=False):
        """One route file plus its ledger line; `ended` is the outcome file's age in seconds."""
        self.n += 1
        route_id = f"rt-{self.n:016x}"
        path = self.root / "routes" / f"{route_id}.json"
        value = {"route_id": route_id, "capability": capability}
        if field:
            value["field"] = field
        path.write_text(json.dumps(value))
        if ended is not None:
            outcome = path.with_name(path.stem + ".outcome.json")
            outcome.write_text("{}")
            os.utime(outcome, (time.time() - ended,) * 2)
        directory = self.root / "chains" / self.harness
        directory.mkdir(parents=True, exist_ok=True)
        with (directory / (session + ".jsonl")).open("a") as ledger:
            ledger.write(json.dumps({
                "v": 1, "ts": time.time() if ts is None else ts, "event": "start", "harness": self.harness,
                "session_id": session, "route_id": route_id, "route_file": str(path),
                "capability": capability, "source_route_id": source,
                **({"automatic": True} if automatic else {})}) + "\n")
        return route_id

    def correction(self, route_id, unit=None, session=SESSION, attempt="att-owner1", at=None):
        path = supervisor_lease_path(self.jobs, attempt).with_suffix(".input.json")
        path.parent.mkdir(parents=True, exist_ok=True)
        item = {"id": "r1", "route_id": route_id, "source_session": session, "state": "queued",
                "events": [{"state": "queued", "time": time.time() if at is None else at}]}
        if unit:
            item["unit"] = unit
        known = json.loads(path.read_text())["requests"] if path.exists() else []
        path.write_text(json.dumps({"schema_version": 1, "attempt_id": attempt, "requests": known + [item]}))

    def unit_correct(self, route_id, unit="qa/code-review", text="look again", session=SESSION,
                     last="pass", parent_ok=True, request_id=None):
        """The real ``correct --route <unit route>`` code path (a fake registry and start, a real text store).

        Returns ``(code, receipt)``; what it keeps is what a later ``mem add`` reads."""
        import work_start
        from dispatch_contract import DispatchContractError
        node, first = "unit", "att-" + "1" * 32
        route = {"route_id": route_id, "route_hash": "h", "capability": "unit", "artifact_root": str(self.root),
                 "cwd": str(self.root / "project"),
                 "nodes": [{"id": node, "dispatch_depth": 1}]}
        meta = {"attempt_id": first, "route_id": route_id, "route_node": node, "harness": self.harness,
                "worker_type": "review", "capability": "unit", "workflow_completion": "runtime-v1",
                "failure_class": last}
        rows = {first: ("done", meta)}
        refuse = None if parent_ok else DispatchContractError("work-parent-recovery-required", "x")
        with mock.patch.dict(os.environ, self.env(**{session_env()[self.harness][0]: session}), clear=True), \
                mock.patch.object(work_start, "unit_leg", return_value={"id": node, "unit": unit}), \
                mock.patch.object(work_start, "_rows", side_effect=lambda jobs: rows), \
                mock.patch.object(work_start, "require_route_parent", side_effect=refuse), \
                mock.patch.object(work_start, "start_work", return_value={"state": "running", "next_step": "n"}):
            return work_start.correct_unit_route(
                route, str(self.root / "routes" / f"{route_id}.json"), self.jobs, text, request_id)

    def direct_turn(self, ago, root="/tmp/proj-a", route_held=False):
        """A direct-work turn that ended ``ago`` seconds back, written the way the real recorder
        keeps it in the session's change-record state file (a folder's later turns leave no ledger line)."""
        path = T.state_path(self.env(), self.harness, SESSION)
        path.parent.mkdir(parents=True, exist_ok=True)
        state = T._read(path) or {"v": T.SCHEMA}
        item = {"ended": time.time() - ago, "source": ["a.py"], "source_count": 1, "section": "changed a.py"}
        if route_held:  # a turn made while a route held the folder: only the held observation
            state["held_turn"] = T._last_turn(item)
        else:
            state.setdefault("records", {})[root] = {"route_file": "-", "route_id": "rt-direct", "closed": True,
                                                       "turns": 2, "last_turn": T._last_turn(item)}
        T._write(path, state)

    def add(self, *extra, rtype="preference", tier="durable", body=None, **env):
        self.n += 1  # a distinct body each time (unless `body` is given), so no write merges into an earlier record
        self.mem("add", tier, rtype, body or f"{BODY} Case {self.n}.", "--scope", "global", *extra, **env)
        with sqlite3.connect(self.root / "store" / "memory.db") as con:
            row = con.execute("SELECT id FROM records ORDER BY rowid DESC LIMIT 1").fetchone()
            if body:
                row = con.execute("SELECT id FROM records WHERE body=?", (body,)).fetchone()
            topics = sorted(t for (t,) in con.execute("SELECT topic FROM record_topics WHERE record_id=?", row))
        return row[0], topics

    def strength(self, rid):
        with sqlite3.connect(self.root / "store" / "memory.db") as con:
            return con.execute("SELECT strength FROM records WHERE id=?", (rid,)).fetchone()[0]

    def found(self, *labels):
        args = ["bootstrap-context", "--cwd", str(self.root / "project")]
        for label in labels:
            args += ["--topic", label]
        return BODY in self.mem(*args).stdout

    def test_one_open_route_names_its_field(self):
        self.route("lab")
        self.assertEqual(self.add()[1], ["field:lab"])

    def test_the_labelled_record_is_found_by_exact_label_only(self):
        self.route("docs")
        self.add()
        self.assertTrue(self.found("field:docs"))
        self.assertFalse(self.found("field:code"))
        self.assertFalse(self.found("unit:qa/code-review"))

    def test_several_routes_or_none_name_nothing(self):
        self.assertEqual(self.add()[1], [])
        self.route("code")
        self.route("docs")
        rid, topics = self.add()
        self.assertEqual(topics, [])
        self.assertTrue(rid, "the record itself is still written")

    def test_a_correction_sent_to_a_stage_adds_its_unit(self):
        rid = self.route("code")
        self.correction(rid, unit="unit:qa/code-review")
        topics = self.add()[1]
        self.assertEqual(topics, ["field:code", "unit:qa/code-review"])
        self.assertTrue(self.found("unit:qa/code-review"))
        self.assertTrue(self.found("field:code"))

    def test_only_this_sessions_newest_correction_on_this_route_counts(self):
        rid = self.route("code")
        other = self.route("docs", session="sess-other")
        self.correction(rid, unit="unit:qa/test", at=100)
        self.correction(rid, unit="unit:qa/code-review", at=200, attempt="att-owner2")
        self.correction(rid, unit="unit:qa/ml-debug", session="sess-other", at=300, attempt="att-owner3")
        self.correction(other, unit="unit:editorial/write", at=400, attempt="att-owner4")
        self.assertEqual(self.add()[1], ["field:code", "unit:qa/code-review"])
        self.correction(rid, at=500, attempt="att-owner5")  # a later correction to the owner
        self.assertEqual(self.add()[1], ["field:code"])

    def test_old_ended_route_and_frame_route_do_not_count(self):
        self.route("code", ended=13 * 3600)
        self.assertEqual(self.add()[1], [])
        self.route("code", ended=60)  # just ended: still the session's route
        self.route(None, capability="route-frame")
        self.assertEqual(self.add()[1], ["field:code"])

    def test_a_route_that_ended_hours_ago_names_nothing(self):
        self.route("research", ended=3600)
        self.assertEqual(self.add()[1], [])
        self.route("research", ended=11 * 60)  # just past the window; one is only ever alone here
        self.assertEqual(self.add()[1], [])

    def test_direct_work_without_a_route_is_not_a_route(self):
        self.route("code", automatic=True, ts=time.time() - 120)  # an automatic change record only
        self.assertEqual(self.add()[1], [])

    def test_direct_work_after_a_route_ended_or_started_names_nothing(self):
        self.route("research", ended=8 * 3600)
        self.route("code", automatic=True, ts=time.time() - 120)
        self.assertEqual(self.add()[1], [])
        self.route("docs", ended=300, ts=time.time() - 600)
        self.route("code", automatic=True)  # direct work after the route ended
        self.assertEqual(self.add()[1], [])

    def test_a_route_started_after_the_direct_work_still_names_its_field(self):
        self.route("code", automatic=True, ts=time.time() - 600)
        self.route("docs", ts=time.time() - 60)
        self.assertEqual(self.add()[1], ["field:docs"])
        self.route("lab", ended=60, ts=time.time() - 900)  # a second route now: several
        self.assertEqual(self.add()[1], [])

    def test_an_open_route_counts_however_long_it_has_run(self):
        self.route("lab", ts=time.time() - 13 * 3600)
        self.assertEqual(self.add()[1], ["field:lab"])  # alone, it names its field
        self.route("docs", ts=time.time() - 3600)
        self.assertEqual(self.add()[1], [])  # with a newer route, nothing

    def test_an_open_route_beyond_the_ledger_tail_still_counts(self):
        self.route("lab", ts=time.time() - 13 * 3600)
        for _ in range(500):  # well over 64 KB of ledger after it
            self.route("code", ended=20 * 3600, ts=time.time() - 20 * 3600)
        self.route("docs", ts=time.time() - 3600)
        self.assertEqual(self.add()[1], [])

    def test_the_same_record_again_keeps_the_given_labels(self):
        text = f"{BODY} Written again."
        first, topics = self.add("--topic", "field:docs", "--topic", "unit:editorial/write", body=text)
        self.assertEqual(topics, ["field:docs", "unit:editorial/write"])
        rid = self.route("code")
        self.correction(rid, unit="unit:qa/code-review")
        again, topics = self.add(body=text)
        self.assertEqual((again, self.strength(first)), (first, 2))  # the same record, reinforced
        self.assertEqual(topics, ["field:docs", "unit:editorial/write"])
        self.add(body=text)
        self.assertEqual(self.add(body=text)[1], ["field:docs", "unit:editorial/write"])

    def test_the_same_record_again_only_adds_a_missing_kind_once(self):
        text = f"{BODY} Unit only."
        self.add("--topic", "unit:editorial/write", body=text)
        rid = self.route("code")
        self.correction(rid, unit="unit:qa/code-review")
        self.assertEqual(self.add(body=text)[1], ["field:code", "unit:editorial/write"])
        self.assertEqual(self.add(body=text)[1], ["field:code", "unit:editorial/write"])
        self.assertEqual(self.add("--topic", "Field:Docs", body=f"{BODY} Cased.")[1],
                         ["field:docs", "unit:qa/code-review"])  # the field given in another case stands

    def test_a_source_upsert_keeps_the_labels_the_record_has(self):
        first, topics = self.add("--topic", "field:docs", "--topic", "unit:editorial/write", "--source", "work-style-from:x",
                                 body=f"{BODY} Moved v1.")
        self.assertEqual(topics, ["field:docs", "unit:editorial/write"])
        rid = self.route("code")
        self.correction(rid, unit="unit:qa/code-review")
        again, topics = self.add("--source", "work-style-from:x", body=f"{BODY} Moved v2.")
        self.assertEqual(again, first)
        self.assertEqual(topics, ["field:docs", "unit:editorial/write"])
        again, topics = self.add("--source", "work-style-from:x", body=f"{BODY} Moved v2.")
        self.assertEqual(topics, ["field:docs", "unit:editorial/write"])

    def test_a_partly_given_topic_does_not_let_the_route_replace_what_the_record_had(self):
        rid = self.route("code")
        self.correction(rid, unit="unit:qa/code-review")
        for given, expected in (("unit:editorial/write", ["unit:editorial/write"]),  # one kind given
                                ("other-topic", ["other-topic"])):  # another topic only
            text = f"{BODY} Partly {given}."
            first = self.add("--topic", "field:docs", "--topic", "unit:editorial/write", body=text,
                             AGENT_DISPATCH_DEPTH="1")  # written with no route labels
            again, topics = self.add("--topic", given, body=text)
            self.assertEqual((again, topics), (first[0], expected))

    def test_a_partly_given_topic_on_a_source_upsert_adds_no_kind_the_record_had(self):
        rid = self.route("code")
        self.correction(rid, unit="unit:qa/code-review")
        for n, (given, expected) in enumerate((("unit:editorial/write", ["unit:editorial/write"]),
                                               ("other-topic", ["other-topic"]))):
            source = f"work-style-from:y{n}"
            first = self.add("--topic", "field:docs", "--topic", "unit:editorial/write", "--source", source,
                             body=f"{BODY} Partly v1 {n}.", AGENT_DISPATCH_DEPTH="1")
            again, topics = self.add("--topic", given, "--source", source, body=f"{BODY} Partly v2 {n}.")
            self.assertEqual((again, topics), (first[0], expected))

    def test_a_later_direct_turn_in_the_same_folder_after_the_route_names_nothing(self):
        self.route("code", automatic=True, ts=time.time() - 3600)  # the folder's first direct turn: a ledger line
        self.direct_turn(3500)
        self.route("research", ended=600, ts=time.time() - 1800)
        self.direct_turn(30)  # its second one, after the route ended: no ledger line, only the state file
        self.assertEqual(self.add()[1], [])

    def test_a_later_direct_turn_after_an_open_route_started_names_nothing(self):
        self.route("code", automatic=True, ts=time.time() - 3600)
        self.route("lab", ts=time.time() - 600)
        self.assertEqual(self.add()[1], ["field:lab"])
        self.direct_turn(100, root="/tmp/proj-b")  # another folder's direct turn while the route runs
        self.assertEqual(self.add()[1], [])

    def test_a_turn_held_by_the_route_is_not_direct_work_and_a_direct_turn_before_it_is_old(self):
        self.direct_turn(3500)
        rid = self.route("code", ts=time.time() - 1800)
        self.direct_turn(60, route_held=True)  # fixed in the route's own folder while it ran
        self.correction(rid, unit="unit:qa/code-review")
        self.assertEqual(self.add()[1], ["field:code", "unit:qa/code-review"])

    def test_an_unreadable_change_record_state_leaves_the_ledger_alone(self):
        self.route("docs", ts=time.time() - 60)
        path = T.state_path(self.env(), self.harness, SESSION)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json")
        self.assertEqual(self.add()[1], ["field:docs"])

    def test_an_open_route_left_behind_by_a_newer_route_names_nothing(self):
        self.route("lab", ts=time.time() - 294 * 3600)  # never closed
        self.route("code", ended=2 * 3600, ts=time.time() - 3 * 3600)
        self.assertEqual(self.add()[1], [])
        self.route("docs", ts=time.time() - 3600)
        self.route("code", ended=1800, ts=time.time() - 3500)  # a second pair: still nothing
        self.assertEqual(self.add()[1], [])

    def test_an_open_route_left_behind_is_not_named_after_a_newer_one_ends(self):
        self.route("lab", ts=time.time() - 13 * 3600)
        self.route("docs", ended=1800, ts=time.time() - 3600)
        self.assertEqual(self.add()[1], [])
        self.route("code", ended=5 * 60, ts=time.time() - 3000)
        self.assertEqual(self.add()[1], [])

    def test_a_continued_route_leaves_only_its_continuation(self):
        first = self.route("code", ended=60)
        self.route("code", source=first)
        self.assertEqual(self.add()[1], ["field:code"])

    def test_legacy_route_without_field_and_unknown_field_name_nothing(self):
        self.route(None)
        self.assertEqual(self.add()[1], [])
        self.route("dev", ended=60)
        self.assertEqual(self.add()[1], [])

    def test_only_a_durable_preference_is_labelled_and_given_labels_stay(self):
        self.route("code")
        self.assertEqual(self.add(rtype="lesson")[1], [])
        self.assertEqual(self.add(tier="working")[1], [])
        self.assertEqual(self.add("--topic", "field:docs")[1], ["field:docs"])
        self.assertEqual(self.add("--topic", "other-topic")[1], ["field:code", "other-topic"])

    def test_a_unit_route_correction_the_runtime_took_adds_its_unit(self):
        rid = self.route("code", capability="unit")
        code, receipt = self.unit_correct(rid)
        self.assertEqual((code, receipt["memory_labels"]), (0, ["field:code", "unit:qa/code-review"]))
        self.assertEqual(self.add()[1], ["field:code", "unit:qa/code-review"])
        self.assertTrue(self.found("unit:qa/code-review"))
        self.assertTrue(self.found("field:code"))
        self.assertFalse(self.found("unit:qa/test"))

    def test_a_unit_correction_that_was_refused_or_never_kept_adds_no_unit(self):
        rid = self.route("code", capability="unit")
        code, receipt = self.unit_correct(rid, parent_ok=False)
        self.assertEqual((code, receipt["state"], "memory_labels" in receipt), (69, "not-admitted", False))
        code, receipt = self.unit_correct(rid, last="blocked")  # the last turn ended with no verdict
        self.assertEqual((code, receipt["state"], "memory_labels" in receipt), (69, "not-admitted", False))
        code, receipt = self.unit_correct(rid, text="  ")  # nothing to keep
        self.assertEqual((code, "memory_labels" in receipt), (69, False))
        self.assertEqual(self.add()[1], ["field:code"])

    def test_a_unit_correction_another_session_sent_does_not_label_this_one(self):
        rid = self.route("code", capability="unit")
        self.unit_correct(rid, session="sess-other")
        self.assertEqual(self.add()[1], ["field:code"])
        self.unit_correct(rid, text="a different ask")  # this session's own, newer
        self.assertEqual(self.add()[1], ["field:code", "unit:qa/code-review"])

    def test_the_newest_unit_correction_of_this_session_wins_and_a_repeat_changes_nothing(self):
        rid = self.route("code", capability="unit")
        self.unit_correct(rid, text="first ask")
        time.sleep(0.2)
        self.unit_correct(rid, unit="qa/test", text="second ask")
        self.assertEqual(self.add()[1], ["field:code", "unit:qa/test"])
        self.unit_correct(rid, unit="qa/ml-debug", text="first ask")  # the same text again: kept once, unchanged
        self.assertEqual(self.add()[1], ["field:code", "unit:qa/test"])

    def test_a_unit_correction_on_one_of_several_routes_or_after_direct_work_names_nothing(self):
        rid = self.route("code", capability="unit")
        self.unit_correct(rid)
        other = self.route("docs", capability="unit")
        self.assertEqual(self.add()[1], [])  # several routes
        self.unit_correct(other, unit="editorial/write")
        self.assertEqual(self.add()[1], [])
        self.direct_turn(1)  # direct work after both
        self.assertEqual(self.add()[1], [])

    def test_a_unit_correction_followed_by_direct_work_names_nothing(self):
        rid = self.route("code", capability="unit", ts=time.time() - 600)
        self.unit_correct(rid)
        self.assertEqual(self.add()[1], ["field:code", "unit:qa/code-review"])
        self.direct_turn(1)
        self.assertEqual(self.add()[1], [])

    def test_a_unit_label_never_replaces_what_the_record_was_given_or_had(self):
        rid = self.route("code", capability="unit")
        self.unit_correct(rid)
        self.assertEqual(self.add("--topic", "unit:editorial/write")[1], ["field:code", "unit:editorial/write"])
        self.assertEqual(self.add("--topic", "field:docs")[1], ["field:docs", "unit:qa/code-review"])
        self.assertEqual(self.add("--topic", "other-topic")[1], ["field:code", "other-topic", "unit:qa/code-review"])
        text = f"{BODY} Unit written again."
        first, topics = self.add("--topic", "unit:editorial/write", body=text, AGENT_DISPATCH_DEPTH="1")
        again, topics = self.add(body=text)
        self.assertEqual((again, topics), (first, ["field:code", "unit:editorial/write"]))
        self.assertEqual(self.add(rtype="lesson")[1], [])
        self.assertEqual(self.add(tier="working")[1], [])

    def test_a_worker_session_gets_no_unit_label_from_a_unit_correction(self):
        rid = self.route("code", capability="unit")
        self.unit_correct(rid)
        self.assertEqual(self.add(AGENT_DISPATCH_DEPTH="2")[1], [])

    def test_a_worker_session_gets_nothing(self):
        self.route("code")
        self.assertEqual(self.add(AGENT_DISPATCH_DEPTH="1")[1], [])
        self.assertEqual(self.add(AGENT_DISPATCH_DEPTH="2")[1], [])


class CorrectionLabelsTest(unittest.TestCase):
    """What the correction device hands back: values only, from the stage it was sent to."""

    def test_a_stage_attempt_names_its_unit_and_an_owner_names_none(self):
        import memory_work_labels as labels
        rows = {"att-stage": {"worker_type": "stage", "dispatch_depth": "2", "unit": "qa/code-review"},
                "att-owner": {"worker_type": "owner", "dispatch_depth": "1", "unit": "_kernel/owner"},
                "att-kernel": {"worker_type": "stage", "dispatch_depth": "2", "unit": "_kernel/owner"}}
        find = lambda _jobs, attempt: rows.get(attempt)  # noqa: E731
        self.assertEqual(labels.stage_unit_label("jobs", "att-stage", find), "unit:qa/code-review")
        self.assertIsNone(labels.stage_unit_label("jobs", "att-owner", find))
        self.assertIsNone(labels.stage_unit_label("jobs", "att-kernel", find))
        self.assertIsNone(labels.stage_unit_label("jobs", "rt-0123456789abcdef", find))

    def test_labels_are_the_sealed_field_plus_the_unit(self):
        import memory_work_labels as labels
        with tempfile.TemporaryDirectory(prefix="mem-labels-", dir="/tmp") as tmp:
            route = Path(tmp) / "route.json"
            route.write_text(json.dumps({"field": "research"}))
            self.assertEqual(labels.correction_labels(route, "unit:qa/test"), ["field:research", "unit:qa/test"])
            self.assertEqual(labels.correction_labels(route), ["field:research"])
            route.write_text(json.dumps({"field": "dev"}))
            self.assertEqual(labels.correction_labels(route), [])
            self.assertEqual(labels.correction_labels(Path(tmp) / "missing.json"), [])


def _per_harness(name):
    return type(f"WorkLabels{name.title()}Test", (WorkLabelsTest,), {"harness": name})


for _name in HARNESSES[1:]:
    globals()[f"WorkLabels{_name.title()}Test"] = _per_harness(_name)


if __name__ == "__main__":
    unittest.main()
