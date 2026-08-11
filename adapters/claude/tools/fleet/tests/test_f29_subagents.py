"""F-29 (v9) — sub-agent observation (prd.md:290-295).

Enrichment ONLY: every test in NoRegressionTest exists to pin prd.md:291's "never touches
session-existence" contract. Fixtures are a throwaway sqlite DB (OpenCode) and throwaway
jsonl transcripts (Claude) — no real opencode/claude state is ever touched.
"""
import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from fleet import collectors as fleet_collectors                  # noqa: E402
from fleet import render                                          # noqa: E402
from fleet.collectors import claude, codex, opencode              # noqa: E402
from fleet.model import Session, SubAgent                         # noqa: E402


class OpenCodeSubagentTest(unittest.TestCase):
    """Temp sqlite DB fixture — real opencode.db never touched."""

    def _db(self, tmp, rows, with_agent_col=True, table_extra=""):
        path = os.path.join(tmp, "opencode.db")
        con = sqlite3.connect(path)
        cols = ["id TEXT", "slug TEXT"]
        if with_agent_col:
            cols.append("agent TEXT")
        cols += ["model TEXT", "cost REAL", "tokens_input INT", "tokens_output INT",
                "tokens_reasoning INT", "time_updated INT", "parent_id TEXT",
                "directory TEXT", "title TEXT"]
        con.execute("CREATE TABLE session (%s)" % ", ".join(cols))
        for r in rows:
            keys = list(r.keys())
            con.execute("INSERT INTO session (%s) VALUES (%s)" %
                       (", ".join(keys), ", ".join("?" for _ in keys)),
                       [r[k] for k in keys])
        con.commit()
        con.close()
        return path

    def test_child_rows_become_subagents(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = self._db(tmp, [
                {"id": "parent1", "slug": "p", "agent": None, "directory": "/x",
                 "time_updated": 1000, "parent_id": None},
                {"id": "child1", "slug": "c1", "agent": "explore", "directory": "/x",
                 "time_updated": 2000, "parent_id": "parent1"},
            ])
            con = sqlite3.connect(db)
            subs = opencode._child_sessions(con, "parent1")
            con.close()
            self.assertEqual(len(subs), 1)
            self.assertEqual(subs[0].agent_type, "explore")

    def test_agent_column_maps_to_type(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = self._db(tmp, [
                {"id": "parent1", "slug": "p", "agent": None, "directory": "/x",
                 "time_updated": 1000, "parent_id": None},
                {"id": "child1", "slug": "c1", "agent": "code-reviewer", "directory": "/x",
                 "time_updated": 2000, "parent_id": "parent1"},
            ])
            con = sqlite3.connect(db)
            subs = opencode._child_sessions(con, "parent1")
            con.close()
            self.assertEqual(subs[0].agent_type, "code-reviewer")
            self.assertEqual(subs[0].source, "opencode-db")

    def test_absent_parent_id_yields_no_subagents(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = self._db(tmp, [
                {"id": "solo1", "slug": "solo", "agent": None, "directory": "/x",
                 "time_updated": 1000, "parent_id": None},
            ])
            con = sqlite3.connect(db)
            subs = opencode._child_sessions(con, "solo1")
            con.close()
            self.assertEqual(subs, [])

    def test_db_without_agent_column_degrades_to_none(self):
        """tolerant (F-3) — an older DB schema must never crash enrich(), and subagents
        stays the honest None (not []): the source itself is unconfirmed, not empty."""
        with tempfile.TemporaryDirectory() as tmp:
            db = self._db(tmp, [
                {"id": "parent1", "slug": "p", "directory": "/x",
                 "time_updated": 1000, "parent_id": None},
            ], with_agent_col=False)
            with mock.patch.dict(os.environ, {"OPENCODE_DB": db}):
                sess = Session(harness="opencode", pid=1, cwd="/x")
                opencode.enrich(sess)
            self.assertIsNone(sess.subagents)

    def test_enrich_wires_subagents_end_to_end(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = self._db(tmp, [
                {"id": "parent1", "slug": "p", "agent": None, "directory": "/x",
                 "time_updated": 1000, "parent_id": None},
                {"id": "child1", "slug": "c1", "agent": "explore", "directory": "/x",
                 "time_updated": 2000, "parent_id": "parent1"},
            ])
            with mock.patch.dict(os.environ, {"OPENCODE_DB": db}):
                sess = Session(harness="opencode", pid=1, cwd="/x")
                opencode.enrich(sess)
            self.assertEqual(len(sess.subagents), 1)
            self.assertEqual(sess.subagents[0].agent_type, "explore")


class CodexSubagentTest(unittest.TestCase):
    """Temp state DB/rollout fixtures — real Codex runtime state is never written."""

    def setUp(self):
        codex._SUBAGENT_INDEX.clear()
        codex._TITLE_INDEX.update(stamp=None, map={})
        codex._PROC_PATHS.clear()
        codex._LIFECYCLE_CACHE.clear()
        codex._LIFECYCLE_CACHE_EVICTIONS = 0

    def tearDown(self):
        codex._SUBAGENT_INDEX.clear()
        codex._TITLE_INDEX.update(stamp=None, map={})
        codex._PROC_PATHS.clear()
        codex._LIFECYCLE_CACHE.clear()
        codex._LIFECYCLE_CACHE_EVICTIONS = 0

    def _db(self, tmp, edges, children, malformed=False):
        path = os.path.join(tmp, "state_5.sqlite")
        con = sqlite3.connect(path)
        if malformed:
            con.execute("CREATE TABLE threads (id TEXT PRIMARY KEY)")
        else:
            con.execute(
                "CREATE TABLE threads (id TEXT PRIMARY KEY, title TEXT, agent_role TEXT, "
                "agent_path TEXT, agent_nickname TEXT, thread_source TEXT, source TEXT, "
                "created_at INTEGER, created_at_ms INTEGER, updated_at INTEGER, "
                "updated_at_ms INTEGER, rollout_path TEXT)"
            )
            con.execute(
                "CREATE TABLE thread_spawn_edges (parent_thread_id TEXT, "
                "child_thread_id TEXT, status TEXT)"
            )
            for child in children:
                rollout_path = os.path.join(tmp, child["id"] + ".jsonl")
                if not child.get("missing_rollout"):
                    lifecycle = child.get("lifecycle", "task_started")
                    events = child.get("events")
                    if events is None:
                        events = [("task_started", "turn-1")]
                        if lifecycle != "task_started":
                            events.append((lifecycle, "turn-1"))
                    with open(rollout_path, "w", encoding="utf-8") as f:
                        for event in events:
                            if isinstance(event, dict):
                                payload = event
                            else:
                                event_type, turn_id = event
                                payload = {"type": event_type, "turn_id": turn_id}
                            f.write(json.dumps(
                                {"type": "event_msg", "payload": payload}) + "\n")
                        if child.get("trailing_bytes"):
                            filler = {"type": "event_msg", "payload": {"type": "note",
                                      "text": "x" * child["trailing_bytes"]}}
                            f.write(json.dumps(filler) + "\n")
                        if child.get("partial"):
                            f.write("[]\n")
                            f.write('{"type":"event_msg","payload":')
                    if child.get("stale"):
                        old = time.time() - 3600
                        os.utime(rollout_path, (old, old))
                con.execute(
                    "INSERT INTO threads (id, agent_role, agent_path, agent_nickname, "
                    "thread_source, source, created_at, created_at_ms, updated_at, "
                    "updated_at_ms, rollout_path) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (child["id"], child.get("agent_role"), child.get("agent_path"),
                     child.get("agent_nickname"), child.get("thread_source", "subagent"),
                     child.get("source", json.dumps({"subagent": {"thread_spawn": {
                         "parent_thread_id": child.get("source_parent") or next(
                             (p for p, c, _s in edges if c == child["id"]), None)}}})),
                     child.get("created_at", 100), child.get("created_at_ms"),
                     child.get("updated_at", int(time.time()) - (3600 if child.get("stale") else 0)),
                     child.get("updated_at_ms",
                               int(time.time() * 1000) - (3600000 if child.get("stale") else 0)),
                     rollout_path),
                )
            con.executemany(
                "INSERT INTO thread_spawn_edges VALUES (?, ?, ?)", edges)
        con.commit()
        con.close()
        return path

    def _lifecycle(self, path, events, trailing=0, between=0):
        with open(path, "w", encoding="utf-8") as handle:
            for index, (event_type, turn_id) in enumerate(events):
                if index and between:
                    handle.write(json.dumps({
                        "type": "event_msg",
                        "payload": {"type": "note", "text": "x" * between},
                    }) + "\n")
                handle.write(json.dumps({
                    "type": "event_msg",
                    "payload": {"type": event_type, "turn_id": turn_id},
                }) + "\n")
            if trailing:
                handle.write(json.dumps({
                    "type": "event_msg",
                    "payload": {"type": "note", "text": "x" * trailing},
                }) + "\n")

    def test_exact_parent_type_and_active_done_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._db(tmp, [
                ("parent", "active-child", "open"),
                ("parent", "done-child", "open"),
            ], [
                {"id": "active-child", "agent_role": "explorer", "agent_path": "/root/a",
                 "created_at_ms": 123000},
                {"id": "done-child", "agent_path": "/root/code-review", "created_at": 456,
                 "lifecycle": "task_complete"},
            ])
            mapped = codex._thread_subagents(tmp)
            self.assertEqual([s.agent_type for s in mapped["parent"]],
                             ["explorer", "code-review"])
            self.assertEqual([s.active for s in mapped["parent"]], [True, False])
            self.assertEqual(mapped["parent"][0].started_at, 123.0)
            self.assertEqual(mapped["parent"][0].source, "codex-state-db")

    def test_child_is_attached_only_to_its_exact_parent(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._db(tmp, [("right-parent", "child", "open")],
                     [{"id": "child", "agent_role": "worker"}])
            mapped = codex._thread_subagents(tmp)
            self.assertIn("right-parent", mapped)
            self.assertNotIn("wrong-parent", mapped)

    def test_latest_lifecycle_is_found_before_a_large_non_lifecycle_tail(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._db(tmp, [("parent", "child", "open")], [{
                "id": "child", "lifecycle": "task_complete", "trailing_bytes": 70000,
            }])
            self.assertFalse(codex._thread_subagents(tmp)["parent"][0].active)

    def test_subagent_start_beyond_session_scan_limit_remains_visible(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "active-large-rollout.jsonl")
            self._lifecycle(
                path,
                [("task_started", "turn")],
                trailing=1048576 + 4096,
            )
            self.assertIsNone(codex._latest_task_lifecycle(path))
            self.assertTrue(codex._subagent_active("open", path))

    def test_subagent_terminal_pair_may_span_more_than_session_scan_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            for terminal in ("task_complete", "turn_aborted"):
                with self.subTest(terminal=terminal):
                    path = os.path.join(tmp, terminal + ".jsonl")
                    self._lifecycle(
                        path,
                        [("task_started", "turn"), (terminal, "turn")],
                        between=1048576 + 4096,
                    )
                    self.assertIsNone(codex._latest_task_lifecycle(path))
                    self.assertIs(codex._subagent_active("open", path), False)

    def test_matching_abort_is_done(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._db(tmp, [("parent", "child", "open")],
                     [{"id": "child", "lifecycle": "turn_aborted"}])
            self.assertFalse(codex._thread_subagents(tmp)["parent"][0].active)

    def test_ordinary_codex_session_enrich_exposes_terminal_lifecycle(self):
        with tempfile.TemporaryDirectory() as tmp:
            rollout = os.path.join(
                tmp,
                "rollout-2026-07-20T00-00-00-"
                "01234567-89ab-cdef-0123-456789abcdef.jsonl",
            )
            with open(rollout, "w", encoding="utf-8") as out:
                for event in ("task_started", "task_complete"):
                    out.write(json.dumps({
                        "type": "event_msg",
                        "payload": {"type": event, "turn_id": "turn-1"},
                    }) + "\n")
            sess = Session(harness="codex", pid=4321, cwd=tmp)
            codex._PROC_PATHS[sess.pid] = rollout
            with mock.patch.object(codex, "_config_model_effort", return_value=(None, None)), \
                 mock.patch.object(codex, "_thread_titles", return_value={}), \
                 mock.patch.object(codex, "_thread_subagents", return_value={}), \
                 mock.patch.object(codex, "_tail_token_count", return_value=None):
                codex.enrich(sess)
        self.assertEqual(sess.task_lifecycle, "task_complete")

    def test_closed_edge_is_done_without_a_rollout(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._db(tmp, [("parent", "child", "closed")],
                     [{"id": "child", "missing_rollout": True}])
            self.assertFalse(codex._thread_subagents(tmp)["parent"][0].active)

    def test_completed_old_turn_then_fresh_new_start_is_active(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._db(tmp, [("parent", "child", "open")], [{"id": "child", "events": [
                ("task_started", "turn-a"), ("task_complete", "turn-a"),
                ("task_started", "turn-b"),
            ]}])
            self.assertTrue(codex._thread_subagents(tmp)["parent"][0].active)

    def test_new_turn_with_matching_completion_is_done(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._db(tmp, [("parent", "child", "open")], [{"id": "child", "events": [
                ("task_started", "turn-a"), ("task_complete", "turn-a"),
                ("task_started", "turn-b"), ("task_complete", "turn-b"),
            ]}])
            self.assertFalse(codex._thread_subagents(tmp)["parent"][0].active)

    def test_stale_unmatched_start_is_omitted(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._db(tmp, [("parent", "child", "open")],
                     [{"id": "child", "stale": True}])
            self.assertNotIn("parent", codex._thread_subagents(tmp))

    def test_mismatched_turn_and_malformed_lifecycle_are_omitted(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._db(tmp, [
                ("parent", "mismatched", "open"),
                ("parent", "malformed", "open"),
            ], [
                {"id": "mismatched", "events": [
                    ("task_started", "turn-a"), ("task_complete", "turn-b")]},
                {"id": "malformed", "events": [
                    ("task_started", "turn-a"), {"type": "task_complete"}]},
            ])
            self.assertNotIn("parent", codex._thread_subagents(tmp))

    def test_partial_final_json_uses_last_well_formed_event(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._db(tmp, [("parent", "child", "open")],
                     [{"id": "child", "partial": True}])
            self.assertTrue(codex._thread_subagents(tmp)["parent"][0].active)

    def test_wal_only_edge_is_visible_and_invalidates_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._db(tmp, [], [])
            self.assertEqual(codex._thread_subagents(tmp), {})
            rollout = os.path.join(tmp, "wal-child.jsonl")
            with open(rollout, "w", encoding="utf-8") as f:
                f.write(json.dumps({"type": "event_msg", "payload": {
                    "type": "task_started", "turn_id": "turn-wal"}}) + "\n")
            con = sqlite3.connect(path)
            con.execute("PRAGMA journal_mode=WAL")
            con.execute(
                "INSERT INTO threads (id, agent_path, thread_source, source, created_at, "
                "updated_at, updated_at_ms, rollout_path) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                ("wal-child", "/root/wal-worker", "subagent",
                 json.dumps({"subagent": {"thread_spawn": {
                     "parent_thread_id": "parent"}}}), 100, int(time.time()),
                 int(time.time() * 1000), rollout),
            )
            con.execute("INSERT INTO thread_spawn_edges VALUES (?, ?, ?)",
                        ("parent", "wal-child", "open"))
            con.commit()
            try:
                mapped = codex._thread_subagents(tmp)
                self.assertEqual(mapped["parent"][0].agent_type, "wal-worker")
                self.assertTrue(mapped["parent"][0].active)
            finally:
                con.close()

    def test_ambiguous_parent_linkage_omits_child(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._db(tmp, [
                ("parent-a", "child", "open"),
                ("parent-b", "child", "open"),
            ], [{"id": "child", "agent_role": "worker"}])
            mapped = codex._thread_subagents(tmp)
            self.assertNotIn("parent-a", mapped)
            self.assertNotIn("parent-b", mapped)

    def test_unknown_status_and_non_subagent_thread_are_omitted(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._db(tmp, [
                ("parent", "unknown", "mystery"),
                ("parent", "root-thread", "open"),
            ], [
                {"id": "unknown", "agent_role": "worker"},
                {"id": "root-thread", "thread_source": "user"},
            ])
            self.assertEqual(codex._thread_subagents(tmp), {})

    def test_source_parent_mismatch_and_malformed_source_are_omitted(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._db(tmp, [
                ("parent", "mismatch", "open"),
                ("parent", "malformed", "closed"),
            ], [
                {"id": "mismatch", "agent_role": "worker", "source_parent": "other"},
                {"id": "malformed", "agent_role": "explorer", "source": "not-json"},
            ])
            self.assertEqual(codex._thread_subagents(tmp), {})

    def test_absent_or_malformed_db_preserves_honest_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(codex._thread_subagents(tmp))
            codex._SUBAGENT_INDEX.clear()
            self._db(tmp, [], [], malformed=True)
            self.assertIsNone(codex._thread_subagents(tmp))

    def test_enrich_wires_subagents_without_changing_session_existence(self):
        parent = "11111111-1111-1111-1111-111111111111"
        with tempfile.TemporaryDirectory() as tmp:
            self._db(tmp, [(parent, "child", "open")],
                     [{"id": "child", "agent_path": "/root/runtime-probe"}])
            rollout = os.path.join(tmp, "rollout-2026-07-20T00-00-00-%s.jsonl" % parent)
            with open(rollout, "w", encoding="utf-8") as f:
                f.write(json.dumps({"type": "session_meta", "payload": {"cwd": "/x"}}) + "\n")
            sess = Session(harness="codex", pid=77, cwd="/x")
            codex._PROC_PATHS[77] = rollout
            with mock.patch.dict(os.environ, {"CODEX_HOME": tmp}):
                codex.enrich(sess)
            self.assertEqual(sess.session_id, parent)
            self.assertEqual(len(sess.subagents), 1)
            self.assertEqual(sess.subagents[0].agent_type, "runtime-probe")

    def test_enrich_uses_the_owned_rollouts_runtime_home(self):
        parent = "22222222-2222-2222-2222-222222222222"
        with tempfile.TemporaryDirectory() as tmp:
            default_home = os.path.join(tmp, "default")
            runtime_home = os.path.join(tmp, "nested")
            rollout_dir = os.path.join(runtime_home, "sessions", "2026", "07", "20")
            os.makedirs(default_home)
            os.makedirs(rollout_dir)
            self._db(runtime_home, [(parent, "child", "closed")],
                     [{"id": "child", "agent_role": "qa-team"}])
            rollout = os.path.join(
                rollout_dir, "rollout-2026-07-20T00-00-00-%s.jsonl" % parent)
            with open(rollout, "w", encoding="utf-8") as f:
                f.write(json.dumps({"type": "session_meta", "payload": {"cwd": "/x"}}) + "\n")
            sess = Session(harness="codex", pid=88, cwd="/x")
            codex._PROC_PATHS[88] = rollout
            with mock.patch.dict(os.environ, {"CODEX_HOME": default_home}):
                codex.enrich(sess)
            self.assertEqual(sess.session_id, parent)
            self.assertEqual(len(sess.subagents), 1)
            self.assertEqual(sess.subagents[0].agent_type, "qa-team")
            self.assertFalse(sess.subagents[0].active)

    def test_lifecycle_cache_hits_for_valid_and_readable_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            valid = os.path.join(tmp, "valid.jsonl")
            ambiguous = os.path.join(tmp, "ambiguous.jsonl")
            self._lifecycle(valid, [("task_started", "turn")])
            self._lifecycle(ambiguous, [("task_complete", "turn")])
            with mock.patch.object(
                codex, "_parse_latest_task_lifecycle",
                wraps=codex._parse_latest_task_lifecycle,
            ) as parse:
                self.assertEqual(
                    codex._latest_task_lifecycle(valid),
                    ("task_started", "turn"),
                )
                self.assertEqual(
                    codex._latest_task_lifecycle(valid),
                    ("task_started", "turn"),
                )
                self.assertIsNone(codex._latest_task_lifecycle(ambiguous))
                self.assertIsNone(codex._latest_task_lifecycle(ambiguous))
            self.assertEqual(parse.call_count, 2)

    def test_lifecycle_parser_configurations_are_isolated_in_both_orders(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "bounded.jsonl")
            self._lifecycle(
                path,
                [("task_started", "turn"), ("task_complete", "turn")],
                trailing=4096,
            )
            for first_narrow in (True, False):
                codex._LIFECYCLE_CACHE.clear()
                calls = (
                    ((64, 128), (65536, 1048576))
                    if first_narrow else
                    ((65536, 1048576), (64, 128))
                )
                with mock.patch.object(
                    codex, "_parse_latest_task_lifecycle",
                    wraps=codex._parse_latest_task_lifecycle,
                ) as parse:
                    results = [
                        codex._latest_task_lifecycle(path, chunk, max_scan)
                        for chunk, max_scan in calls
                    ]
                expected = (
                    [None, ("task_complete", "turn")]
                    if first_narrow else
                    [("task_complete", "turn"), None]
                )
                self.assertEqual(results, expected)
                self.assertEqual(parse.call_count, 2)

    def test_lifecycle_chunk_is_identity_and_global_lru_is_bounded(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "active.jsonl")
            self._lifecycle(path, [("task_started", "turn")])
            with mock.patch.object(
                codex, "_parse_latest_task_lifecycle",
                wraps=codex._parse_latest_task_lifecycle,
            ) as parse:
                for chunk in range(1, codex._LIFECYCLE_CACHE_MAX + 2):
                    codex._latest_task_lifecycle(path, chunk=chunk)
            self.assertEqual(parse.call_count, codex._LIFECYCLE_CACHE_MAX + 1)
            self.assertEqual(len(codex._LIFECYCLE_CACHE), codex._LIFECYCLE_CACHE_MAX)
            first = (os.path.realpath(path), 1, 1048576)
            newest = (
                os.path.realpath(path), codex._LIFECYCLE_CACHE_MAX + 1, 1048576
            )
            self.assertNotIn(first, codex._LIFECYCLE_CACHE)
            self.assertIn(newest, codex._LIFECYCLE_CACHE)

    def test_lifecycle_append_replacement_and_io_failure_invalidate(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "changing.jsonl")
            self._lifecycle(path, [("task_started", "turn")])
            self.assertEqual(
                codex._latest_task_lifecycle(path), ("task_started", "turn")
            )
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps({
                    "type": "event_msg",
                    "payload": {"type": "task_complete", "turn_id": "turn"},
                }) + "\n")
            self.assertEqual(
                codex._latest_task_lifecycle(path), ("task_complete", "turn")
            )
            replacement = os.path.join(tmp, "replacement.jsonl")
            self._lifecycle(replacement, [("task_started", "replacement")])
            os.replace(replacement, path)
            self.assertEqual(
                codex._latest_task_lifecycle(path),
                ("task_started", "replacement"),
            )
            key = (os.path.realpath(path), 65536, 1048576)
            with mock.patch.object(codex.os, "stat", side_effect=OSError):
                self.assertIsNone(codex._latest_task_lifecycle(path))
            self.assertNotIn(key, codex._LIFECYCLE_CACHE)

    def test_one_changed_rollout_reparses_only_that_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            changed = os.path.join(tmp, "changed.jsonl")
            unchanged = os.path.join(tmp, "unchanged.jsonl")
            self._lifecycle(changed, [("task_started", "changed")])
            self._lifecycle(unchanged, [("task_started", "unchanged")])
            codex._latest_task_lifecycle(changed)
            codex._latest_task_lifecycle(unchanged)
            with open(changed, "a", encoding="utf-8") as handle:
                handle.write(json.dumps({
                    "type": "event_msg",
                    "payload": {
                        "type": "task_complete", "turn_id": "changed"
                    },
                }) + "\n")
            with mock.patch.object(
                codex, "_parse_latest_task_lifecycle",
                wraps=codex._parse_latest_task_lifecycle,
            ) as parse:
                self.assertEqual(
                    codex._latest_task_lifecycle(changed),
                    ("task_complete", "changed"),
                )
                self.assertEqual(
                    codex._latest_task_lifecycle(unchanged),
                    ("task_started", "unchanged"),
                )
            parse.assert_called_once()
            self.assertEqual(parse.call_args.args[0], os.path.realpath(changed))

    def test_lifecycle_open_failure_removes_stale_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "failure.jsonl")
            self._lifecycle(path, [("task_started", "turn")])
            codex._latest_task_lifecycle(path)
            key = (os.path.realpath(path), 65536, 1048576)
            # Force a stamp miss so the wrapper reaches the raw open, then fail it.
            stat = os.stat(path)
            os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1))
            with mock.patch.object(
                codex, "_parse_latest_task_lifecycle", side_effect=OSError
            ):
                self.assertIsNone(codex._latest_task_lifecycle(path))
            self.assertNotIn(key, codex._LIFECYCLE_CACHE)

    def test_lifecycle_changed_during_read_is_not_cached(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "changing.jsonl")
            self._lifecycle(path, [("task_started", "turn")])
            raw = codex._parse_latest_task_lifecycle

            def mutate(canonical, chunk, max_scan):
                result = raw(canonical, chunk, max_scan)
                with open(canonical, "a", encoding="utf-8") as handle:
                    handle.write("{}\n")
                return result

            with mock.patch.object(
                codex, "_parse_latest_task_lifecycle", side_effect=mutate
            ):
                self.assertIsNone(codex._latest_task_lifecycle(path))
            key = (os.path.realpath(path), 65536, 1048576)
            self.assertNotIn(key, codex._LIFECYCLE_CACHE)

    def test_symlink_retarget_binds_one_canonical_target_per_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            target_a = os.path.join(tmp, "a.jsonl")
            target_b = os.path.join(tmp, "b.jsonl")
            link = os.path.join(tmp, "current.jsonl")
            self._lifecycle(target_a, [("task_started", "a")])
            self._lifecycle(
                target_b,
                [("task_started", "b"), ("task_complete", "b")],
            )
            os.symlink(target_a, link)
            raw = codex._parse_latest_task_lifecycle
            parsed_paths = []

            def retarget(canonical, chunk, max_scan):
                parsed_paths.append(canonical)
                os.unlink(link)
                os.symlink(target_b, link)
                return raw(canonical, chunk, max_scan)

            with mock.patch.object(
                codex, "_parse_latest_task_lifecycle", side_effect=retarget
            ):
                self.assertEqual(
                    codex._latest_task_lifecycle(link), ("task_started", "a")
                )
            self.assertEqual(parsed_paths, [os.path.realpath(target_a)])
            self.assertEqual(
                codex._latest_task_lifecycle(link), ("task_complete", "b")
            )

    def test_canonical_target_replacement_during_read_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = os.path.join(tmp, "target.jsonl")
            link = os.path.join(tmp, "current.jsonl")
            self._lifecycle(target, [("task_started", "old")])
            os.symlink(target, link)
            raw = codex._parse_latest_task_lifecycle

            def replace(canonical, chunk, max_scan):
                result = raw(canonical, chunk, max_scan)
                replacement = os.path.join(tmp, "new.jsonl")
                self._lifecycle(replacement, [("task_started", "new")])
                os.replace(replacement, canonical)
                return result

            with mock.patch.object(
                codex, "_parse_latest_task_lifecycle", side_effect=replace
            ):
                self.assertIsNone(codex._latest_task_lifecycle(link))
            key = (os.path.realpath(target), 65536, 1048576)
            self.assertNotIn(key, codex._LIFECYCLE_CACHE)
            self.assertEqual(
                codex._latest_task_lifecycle(link), ("task_started", "new")
            )

    def test_tick_snapshot_freezes_db_change_until_next_tick(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._db(
                tmp,
                [("parent", "first", "closed")],
                [{"id": "first", "agent_role": "first", "missing_rollout": True}],
            )
            first_tick = codex._CodexTick(tmp, {}, {})
            first = codex._tick_subagents(first_tick, tmp)
            connection = sqlite3.connect(path)
            source = json.dumps({"subagent": {"thread_spawn": {
                "parent_thread_id": "parent"
            }}})
            connection.execute(
                "INSERT INTO threads (id, agent_role, thread_source, source, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                ("second", "second", "subagent", source, 100, int(time.time())),
            )
            connection.execute(
                "INSERT INTO thread_spawn_edges VALUES (?, ?, ?)",
                ("parent", "second", "closed"),
            )
            connection.commit()
            connection.close()
            self.assertEqual(
                [
                    item.agent_type
                    for item in codex._tick_subagents(first_tick, tmp)["parent"]
                ],
                ["first"],
            )
            second_tick = codex._CodexTick(tmp, {}, {})
            second = codex._tick_subagents(second_tick, tmp)
            self.assertEqual(
                [item.agent_type for item in first["parent"]], ["first"]
            )
            self.assertEqual(
                [item.agent_type for item in second["parent"]],
                ["first", "second"],
            )

    def test_tick_home_cache_preserves_none_empty_and_one_build_per_home(self):
        tick = codex._CodexTick("/default", {}, {})
        with mock.patch.object(
            codex, "_thread_subagents", side_effect=[None, {}, {"parent": []}]
        ) as build:
            self.assertIsNone(codex._tick_subagents(tick, "/unavailable"))
            self.assertIsNone(codex._tick_subagents(tick, "/unavailable"))
            self.assertEqual(codex._tick_subagents(tick, "/empty"), {})
            self.assertEqual(codex._tick_subagents(tick, "/empty"), {})
            self.assertEqual(
                codex._tick_subagents(tick, "/unexpected"), {"parent": []}
            )
            self.assertEqual(
                codex._tick_subagents(tick, "/unexpected"), {"parent": []}
            )
        self.assertEqual(build.call_count, 3)

    def test_prepare_tick_prewarms_default_and_owned_runtime_homes(self):
        default = "/runtime/default"
        nested = (
            "/runtime/nested/sessions/2033/05/18/"
            "rollout-2033-05-18T00-00-00-"
            "33333333-3333-3333-3333-333333333333.jsonl"
        )
        sessions = [
            Session(harness="codex", pid=1, cwd="/a"),
            Session(harness="codex", pid=2, cwd="/b"),
        ]
        with mock.patch.object(codex, "_home", return_value=default), \
             mock.patch.object(
                 codex, "_proc_rollout", side_effect=[None, nested]
             ), \
             mock.patch.object(codex, "_thread_subagents", return_value={}) as build:
            tick = codex.prepare_tick(sessions)
        self.assertEqual(tick.default_home, os.path.abspath(default))
        self.assertEqual(tick.proc_paths, {2: nested})
        self.assertEqual(
            set(tick.subagents_by_home),
            {os.path.abspath(default), "/runtime/nested"},
        )
        self.assertEqual(build.call_count, 2)

    def test_collect_all_passes_tick_only_to_codex_enrichment(self):
        codex_session = Session(harness="codex", pid=1, cwd="/codex")
        claude_session = Session(harness="claude", pid=2, cwd="/claude")
        tick = codex._CodexTick("/default", {}, {})
        with mock.patch.object(
            fleet_collectors.procscan, "scan",
            return_value=[codex_session, claude_session],
        ), mock.patch.object(codex, "prepare_tick", return_value=tick), \
             mock.patch.object(codex, "enrich") as codex_enrich, \
             mock.patch.object(claude, "enrich") as claude_enrich, \
             mock.patch("fleet.collectors.usage_api.account_usage", return_value=None), \
             mock.patch.object(codex, "account_usage", return_value=None), \
             mock.patch("fleet.collectors.liveness.classify", return_value="working"), \
             mock.patch("fleet.collectors.dispatch.collect", return_value=[]):
            fleet_collectors.collect_all()
        codex_enrich.assert_called_once_with(codex_session, tick=tick)
        claude_enrich.assert_called_once_with(claude_session)


def _tool_use_line(tid, agent_type, ts="2026-07-15T10:00:00Z", name="Task"):
    return {"type": "assistant", "isSidechain": False, "timestamp": ts,
            "message": {"role": "assistant", "content": [
                {"type": "tool_use", "id": tid, "name": name,
                 "input": {"subagent_type": agent_type}}]}}


def _tool_result_line(tid, ts="2026-07-15T10:05:00Z", content="done"):
    return {"type": "user", "isSidechain": False, "timestamp": ts,
            "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": tid, "content": content}]}}


def _async_ack_line(tid, agent_id, ts="2026-07-15T10:00:01Z"):
    """The harness's immediate answer to a background Agent launch — NOT completion."""
    return _tool_result_line(tid, ts=ts, content=(
        "Async agent launched successfully. agentId: %s (internal ID)." % agent_id))


def _task_notification_line(agent_id, ts="2026-07-15T10:07:00Z"):
    """The user-turn stop notification the harness injects when a background agent ends."""
    return {"type": "user", "isSidechain": False, "timestamp": ts,
            "message": {"role": "user", "content":
                "<task-notification><task-id>%s</task-id>"
                "<status>completed</status></task-notification>" % agent_id}}


def _write_transcript(path, lines):
    with open(path, "w", encoding="utf-8") as f:
        for ln in lines:
            f.write(json.dumps(ln) + "\n")


class ClaudeSidechainTest(unittest.TestCase):
    """Temp jsonl transcript fixture — real ~/.claude never touched."""

    def setUp(self):
        claude._SUBAGENT_CACHE.clear()

    def test_sidechain_lines_pair_tool_use_and_tool_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "t.jsonl")
            _write_transcript(path, [
                _tool_use_line("t1", "explore"),
                _tool_result_line("t1"),
            ])
            subs = claude._tail_subagents(path)
            self.assertEqual(len(subs), 1)
            self.assertFalse(subs[0].active, "paired tool_use/tool_result = completed")

    def test_unpaired_tool_use_counts_as_active(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "t.jsonl")
            _write_transcript(path, [_tool_use_line("t1", "code-reviewer")])
            subs = claude._tail_subagents(path)
            self.assertEqual(len(subs), 1)
            self.assertTrue(subs[0].active)
            self.assertEqual(subs[0].agent_type, "code-reviewer")

    def test_current_runtime_agent_tool_name_is_matched(self):
        """claude.py:197 matched tool_use name == "Task" only; current runtimes emit
        "Agent" instead, so this collector counted 0 sub-agents against every live
        transcript until fixed — regression fixture for that drift."""
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "t.jsonl")
            _write_transcript(path, [_tool_use_line("t1", "explore", name="Agent")])
            subs = claude._tail_subagents(path)
            self.assertEqual(len(subs), 1)
            self.assertTrue(subs[0].active)
            self.assertEqual(subs[0].agent_type, "explore")

    def test_task_and_agent_names_both_pair_with_tool_result(self):
        """Old ("Task") and current ("Agent") transcripts must both resolve to completed
        when a tool_result answers them — compatibility, not an either/or switch."""
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "t.jsonl")
            _write_transcript(path, [
                _tool_use_line("t1", "explore", name="Task"),
                _tool_result_line("t1"),
                _tool_use_line("t2", "code-reviewer", name="Agent", ts="2026-07-15T10:01:00Z"),
                _tool_result_line("t2", ts="2026-07-15T10:06:00Z"),
            ])
            subs = claude._tail_subagents(path)
            self.assertEqual(len(subs), 2)
            self.assertFalse(subs[0].active)
            self.assertFalse(subs[1].active)

    def test_async_launch_ack_keeps_the_agent_active(self):
        """Background agents answer their tool_use IMMEDIATELY with a launch ack while the
        agent keeps running — pairing alone misread every async agent as completed on the
        spot, so the active ● state never showed live (user 2026-07-16)."""
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "t.jsonl")
            _write_transcript(path, [
                _tool_use_line("t1", "explore", name="Agent"),
                _async_ack_line("t1", "a1b2c3d4e5"),
            ])
            subs = claude._tail_subagents(path)
            self.assertEqual(len(subs), 1)
            self.assertTrue(subs[0].active, "launch ack ≠ completion")

    def test_async_agent_completes_on_task_notification(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "t.jsonl")
            _write_transcript(path, [
                _tool_use_line("t1", "explore", name="Agent"),
                _async_ack_line("t1", "a1b2c3d4e5"),
                _task_notification_line("a1b2c3d4e5"),
            ])
            subs = claude._tail_subagents(path)
            self.assertEqual(len(subs), 1)
            self.assertFalse(subs[0].active)

    def test_foreign_notification_does_not_complete_the_agent(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "t.jsonl")
            _write_transcript(path, [
                _tool_use_line("t1", "explore", name="Agent"),
                _async_ack_line("t1", "a1b2c3d4e5"),
                _task_notification_line("other-task-id"),
            ])
            subs = claude._tail_subagents(path)
            self.assertTrue(subs[0].active)

    def test_async_ack_without_id_falls_back_to_completed(self):
        # Untrackable launch (marker but no parseable agentId) fails toward the pre-async
        # pairing — completed — rather than showing an active row forever.
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "t.jsonl")
            _write_transcript(path, [
                _tool_use_line("t1", "explore", name="Agent"),
                _tool_result_line("t1", content="Async agent launched successfully."),
            ])
            subs = claude._tail_subagents(path)
            self.assertFalse(subs[0].active)

    def test_malformed_lines_are_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "t.jsonl")
            with open(path, "w", encoding="utf-8") as f:
                f.write('{"type":"assistant","message":{"content":[{"type":"tool_use"\n')
                f.write("not even json at all\n")
                f.write(json.dumps(_tool_use_line("t2", "explore")) + "\n")
            subs = claude._tail_subagents(path)
            self.assertEqual(len(subs), 1)
            self.assertEqual(subs[0].agent_type, "explore")

    def test_cache_keyed_on_mtime_and_size(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "t.jsonl")
            _write_transcript(path, [_tool_use_line("t1", "explore")])
            first = claude._tail_subagents(path)
            self.assertEqual(len(first), 1)
            st = os.stat(path)
            claude._SUBAGENT_CACHE[path] = (st.st_mtime, st.st_size, [])   # poison the cache
            cached = claude._tail_subagents(path)
            self.assertEqual(cached, [], "재읽기 없이 (mtime,size) 캐시를 그대로 반환해야 함")


class NoRegressionTest(unittest.TestCase):
    """prd.md:291/293/294 — enrichment cannot touch existence, pulse, or --json shape."""

    def setUp(self):
        render.reset_selection()

    def _rows(self, subagents=None):
        s = Session(harness="claude", pid=90001, cwd="/x", slug="a",
                   liveness="working", title="live", elapsed_min=5)
        s.proc_start = "111"
        s.subagents = subagents
        return [s]

    def test_source_absent_omits_subrow_entirely(self):
        lines = render._build_lines(self._rows(subagents=None), [], section="fleet",
                                    narrow=False, malformed=0, term_width=168)
        joined = "\n".join("".join(t for t, _k in ln) for ln in lines if ln)
        self.assertNotIn(render._ICON_SUBAGENT, joined)

    def test_subagents_render_as_one_horizontal_strip_row(self):
        """F-29 v11 (사용자 확정 2026-07-16) — 2+ sub-agents render as ONE horizontal strip
        row per session (`⚡ type ● elapsed · type ● elapsed …`), replacing the old
        one-row-per-subagent ├⚡/└⚡ stack."""
        subs = [SubAgent(agent_type="explore", active=True),
               SubAgent(agent_type="code-reviewer", active=True),
               SubAgent(agent_type="fact-check", active=True)]
        lines = render._build_lines(self._rows(subagents=subs), [], section="fleet",
                                    narrow=False, malformed=0, term_width=168)
        sub_lines = [ln for ln in lines
                    if ln and "explore" in "".join(t for t, _k in ln)]
        self.assertEqual(len(sub_lines), 1, "3개 서브에이전트가 한 줄 스트립이어야 함")
        text = "".join(t for t, _k in sub_lines[0])
        self.assertIn("explore", text)
        self.assertIn("code-reviewer", text)
        self.assertIn("fact-check", text)
        self.assertIn(" · ", text, "가로 나열은 가운뎃점으로 구분")
        self.assertNotIn("├", text)
        self.assertNotIn("└", text)

    def test_strip_hides_completed_by_default_shows_with_show_all(self):
        subs = [SubAgent(agent_type="explore", active=True),
               SubAgent(agent_type="fact-check", active=False)]
        try:
            lines = render._build_lines(self._rows(subagents=subs), [], section="fleet",
                                        narrow=False, malformed=0, term_width=168)
            text = "".join("".join(t for t, _k in ln) for ln in lines if ln)
            self.assertIn("explore", text)
            self.assertNotIn("fact-check", text)
            render.set_show_all(True)
            lines = render._build_lines(self._rows(subagents=subs), [], section="fleet",
                                        narrow=False, malformed=0, term_width=168)
            text = "".join("".join(t for t, _k in ln) for ln in lines if ln)
            self.assertIn("fact-check", text)
        finally:
            render.set_show_all(False)

    def test_strip_indent_is_a_deep_pure_inset(self):
        """`_SUBAGENT_IND` stays a pure inset (spaces only, no connector) and lands past
        the F-64c rail column (사용자 2026-07-16 '들여쓰기 레벨을 충분히 안쪽으로' — both
        the 2-cell and 4-cell insets read as siblings, not children). depth-1 rows are
        arrowless since F-64c, so the anchor is the rail column the strip must clear."""
        self.assertEqual(render._SUBAGENT_IND.strip(), "")
        self.assertGreaterEqual(len(render._SUBAGENT_IND), render._RAIL_COL + 2)

    def test_strip_depth_pushes_the_inset_further_inward(self):
        """A dispatch-owned strip (depth ≥ 1) indents 2 more cells per level than a
        session-owned one, so the strip always reads inside its OWN owner row."""
        sub = [SubAgent(agent_type="explore", active=True)]
        session_lead = render._subagent_strip(sub, depth=0)[0][0][0]
        d1_lead = render._subagent_strip(sub, depth=1)[0][0][0]
        d2_lead = render._subagent_strip(sub, depth=2)[0][0][0]
        self.assertEqual(len(d1_lead), len(session_lead) + 2)
        self.assertEqual(len(d2_lead), len(session_lead) + 4)

    def test_dispatch_row_carries_its_child_sessions_strip(self):
        """서브 세션(분사 자식)의 서브에이전트도 뜬다 (사용자 2026-07-16): the hidden child
        session's subagents re-attach as a strip under the dispatch row that represents
        it, joined by pid — same join as title adoption."""
        from fleet.model import DispatchJob
        parent = Session(harness="claude", pid=1, cwd="/x", session_id="parent-sid",
                         slug="x", liveness="working", title="parent")
        child = Session(harness="claude", pid=42, cwd="/x/wt", slug="wt",
                        liveness="working", is_child=True)
        child.subagents = [SubAgent(agent_type="fact-check", active=True)]
        job = DispatchJob(key="autopilot-code", slug="wt", cwd="/x/wt", harness="claude",
                          pid=42, is_child=True, parent_sid="parent-sid",
                          liveness="working")
        lines = render._build_lines([parent, child], [job], section="both",
                                    narrow=False, malformed=0, term_width=168)
        strip = [ln for ln in lines
                 if ln and render._ICON_SUBAGENT in "".join(t for t, _k in ln)
                 and "fact-check" in "".join(t for t, _k in ln)]
        self.assertEqual(len(strip), 1)
        text = "".join(t for t, _k in strip[0] if not render._is_fill(t))
        lead = text[:text.index(render._ICON_SUBAGENT)].lstrip("▍")
        # F-64c: the strip belongs to a depth-1 dispatch unit, so its lead may carry the
        # unit's vertical rail — pure inset otherwise.
        self.assertIn(lead.strip(), ("",) + render._RAIL_CHARS)
        # depth-1 strip = _SUBAGENT_IND + 2, minus the one indent cell the group tint
        # rail (▍) consumes when it is prepended to every card row.
        self.assertGreaterEqual(len(lead), len(render._SUBAGENT_IND) + 1)

    def test_no_subagent_count_badge_on_the_session_name_row(self):
        """The ⚡N name-zone badge is retired — the strip is inline under the row, so a
        second count on the name line would be redundant (사용자 확정 2026-07-16)."""
        subs = [SubAgent(agent_type="explore", active=True),
               SubAgent(agent_type="code-reviewer", active=True)]
        lines = render._build_lines(self._rows(subagents=subs), [], section="fleet",
                                    narrow=False, malformed=0, term_width=168)
        name_lines = [ln for ln in lines
                     if ln and any(k == "name_work" for _t, k in ln)]
        self.assertEqual(len(name_lines), 1)
        name_text = "".join(t for t, _k in name_lines[0])
        self.assertNotIn("⚡2", name_text)
        self.assertNotIn(render._ICON_SUBAGENT, name_text)

    def test_parse_failure_omits_subrow_entirely(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "opencode.db")
            con = sqlite3.connect(db)
            con.execute("CREATE TABLE session (id TEXT)")   # missing every expected column
            con.commit()
            con.close()
            with mock.patch.dict(os.environ, {"OPENCODE_DB": db}):
                sess = Session(harness="opencode", pid=1, cwd="/x")
                opencode.enrich(sess)          # must not raise
            self.assertIsNone(sess.subagents)

    def test_subagents_never_enter_pulse_counts(self):
        active = [SubAgent(agent_type="explore", active=True)]
        with_subs = self._rows(subagents=active)
        without_subs = self._rows(subagents=None)
        # Freeze the animation frame so a 100 ms scheduler boundary cannot make
        # this semantic comparison fail on spinner glyph alone.
        with mock.patch.object(render.time, "time", return_value=1234.5):
            lines_with = render._build_lines(
                with_subs, [], section="fleet", narrow=False,
                malformed=0, term_width=168,
            )
            lines_without = render._build_lines(
                without_subs, [], section="fleet", narrow=False,
                malformed=0, term_width=168,
            )
        def pulse(lines):
            return next(
                "".join(text for text, _key in line)
                for line in lines if line
                if "".join(text for text, _key in line).lstrip().startswith("fleet ")
            )

        pulse_with = pulse(lines_with)
        pulse_without = pulse(lines_without)
        self.assertEqual(pulse_with, pulse_without,
                         "서브에이전트 존재가 fleet pulse 줄을 바꿨다")

    def test_session_existence_unaffected_by_subagents(self):
        """prd.md:291 — classify_session doesn't even see the field; subagents lives on
        Session AFTER classification, purely as enrichment."""
        import inspect
        from fleet import model
        self.assertNotIn("subagents", inspect.getsource(model.classify_session))

    def test_json_key_is_additive(self):
        plain = Session(harness="claude", pid=1, cwd="/x").to_dict()
        self.assertIn("subagents", plain)
        self.assertIsNone(plain["subagents"])
        # every other key from a bare Session must still be there — additive, not replaced.
        self.assertIn("pid", plain)
        self.assertIn("liveness", plain)


class ClaudeSubagentBudgetTest(unittest.TestCase):
    """`subagents/agent-*.meta.json` toolUseId join — actual model/effort enrichment
    (사용자 2026-07-29). Same fixture discipline as ClaudeSidechainTest: throwaway
    tmp transcripts, real ~/.claude never touched."""

    def setUp(self):
        claude._SUBAGENT_CACHE.clear()
        claude._SUBAGENT_BUDGET_CACHE.clear()

    def _fixture(self, tmp, meta=None, turns=None):
        path = os.path.join(tmp, "sess.jsonl")
        _write_transcript(path, [_tool_use_line("t1", "general-purpose", name="Agent")])
        sub_dir = os.path.join(tmp, "sess", "subagents")
        os.makedirs(sub_dir)
        if meta is not None:
            with open(os.path.join(sub_dir, "agent-abc.meta.json"), "w", encoding="utf-8") as f:
                json.dump(meta, f)
        if turns is not None:
            with open(os.path.join(sub_dir, "agent-abc.jsonl"), "w", encoding="utf-8") as f:
                for t in turns:
                    f.write(json.dumps(t) + "\n")
        return path

    def test_meta_join_fills_actual_model_and_effort(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._fixture(
                tmp,
                meta={"agentType": "general-purpose", "toolUseId": "t1",
                      "spawnDepth": 1, "model": "sonnet"},
                turns=[{"type": "assistant", "effort": "xhigh",
                        "message": {"model": "claude-sonnet-5"}}])
            subs = claude._tail_subagents(path)
            self.assertEqual(len(subs), 1)
            self.assertEqual(subs[0].model, "claude-sonnet-5")
            self.assertEqual(subs[0].effort, "xhigh")

    def test_paired_tool_result_timestamp_becomes_ended_at(self):
        """완료 시각 (사용자 2026-07-29 '언제 끝났는지'): the resolving tool_result's
        own timestamp is the completion time."""
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "sess.jsonl")
            _write_transcript(path, [
                _tool_use_line("t1", "explore", name="Agent", ts="2026-07-15T10:00:00Z"),
                _tool_result_line("t1", ts="2026-07-15T10:05:00Z"),
            ])
            subs = claude._tail_subagents(path)
            self.assertFalse(subs[0].active)
            self.assertEqual(subs[0].ended_at, claude._ts_to_epoch("2026-07-15T10:05:00Z"))

    def test_async_notification_timestamp_becomes_ended_at(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "sess.jsonl")
            _write_transcript(path, [
                _tool_use_line("t1", "explore", name="Agent"),
                _async_ack_line("t1", "a1b2c3d4e5"),
                _task_notification_line("a1b2c3d4e5", ts="2026-07-15T10:07:00Z"),
            ])
            subs = claude._tail_subagents(path)
            self.assertFalse(subs[0].active)
            self.assertEqual(subs[0].ended_at, claude._ts_to_epoch("2026-07-15T10:07:00Z"))

    def test_active_entry_has_no_ended_at(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "sess.jsonl")
            _write_transcript(path, [_tool_use_line("t1", "explore", name="Agent")])
            subs = claude._tail_subagents(path)
            self.assertTrue(subs[0].active)
            self.assertIsNone(subs[0].ended_at)

    def test_budget_join_mtime_fallback_fills_missing_ended_at(self):
        """A resolving record with no timestamp leaves ended_at None; the sub-agent's
        own transcript mtime (its actual last activity) is the honest fallback."""
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "sess.jsonl")
            no_ts = {"type": "user", "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": "done"}]}}
            _write_transcript(path, [
                _tool_use_line("t1", "general-purpose", name="Agent"), no_ts])
            sub_dir = os.path.join(tmp, "sess", "subagents")
            os.makedirs(sub_dir)
            with open(os.path.join(sub_dir, "agent-abc.meta.json"), "w", encoding="utf-8") as f:
                json.dump({"agentType": "general-purpose", "toolUseId": "t1",
                           "spawnDepth": 1, "model": "sonnet"}, f)
            jl = os.path.join(sub_dir, "agent-abc.jsonl")
            with open(jl, "w", encoding="utf-8") as f:
                f.write(json.dumps({"type": "assistant", "effort": "medium",
                                    "message": {"model": "claude-sonnet-5"}}) + "\n")
            subs = claude._tail_subagents(path)
            self.assertFalse(subs[0].active)
            self.assertEqual(subs[0].ended_at, os.stat(jl).st_mtime)

    def test_missing_subagents_dir_leaves_honest_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "sess.jsonl")
            _write_transcript(path, [_tool_use_line("t1", "explore", name="Agent")])
            subs = claude._tail_subagents(path)
            self.assertEqual(len(subs), 1)
            self.assertIsNone(subs[0].model)
            self.assertIsNone(subs[0].effort)

    def test_transcript_without_assistant_falls_back_to_meta_alias(self):
        """A just-launched sub-agent has a meta file but no assistant turn yet — the
        requested alias is honest evidence (it IS what was requested); effort stays
        None (nothing observed, prd.md:292 no-guessing)."""
        with tempfile.TemporaryDirectory() as tmp:
            path = self._fixture(
                tmp,
                meta={"agentType": "general-purpose", "toolUseId": "t1",
                      "spawnDepth": 1, "model": "sonnet"},
                turns=[{"type": "user", "message": {"content": "hi"}}])
            subs = claude._tail_subagents(path)
            self.assertEqual(subs[0].model, "sonnet")
            self.assertIsNone(subs[0].effort)

    def test_foreign_tool_use_id_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._fixture(
                tmp,
                meta={"agentType": "general-purpose", "toolUseId": "other-tid",
                      "spawnDepth": 1, "model": "sonnet"},
                turns=[{"type": "assistant", "effort": "xhigh",
                        "message": {"model": "claude-sonnet-5"}}])
            subs = claude._tail_subagents(path)
            self.assertIsNone(subs[0].model)
            self.assertIsNone(subs[0].effort)

    def test_malformed_meta_is_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._fixture(tmp)
            sub_dir = os.path.join(tmp, "sess", "subagents")
            with open(os.path.join(sub_dir, "agent-abc.meta.json"), "w", encoding="utf-8") as f:
                f.write("{not json")
            subs = claude._tail_subagents(path)
            self.assertEqual(len(subs), 1)
            self.assertIsNone(subs[0].model)


class SubagentStripBudgetTest(unittest.TestCase):
    """Strip rendering of the model/effort parenthetical and the blinking green dot
    (사용자 2026-07-29: 흰 점 → 점멸 녹색 점 + 모델명)."""

    def _strip_text(self, subs):
        return "".join(t for t, _k in render._subagent_strip(subs)[0])

    def _strip_keys(self, subs):
        return render._subagent_strip(subs)[0]

    def test_budget_parenthetical_shows_model_and_short_effort(self):
        text = self._strip_text([SubAgent(agent_type="general-purpose", active=True,
                                          model="claude-sonnet-5", effort="xhigh")])
        self.assertIn("(Sonnet 5·xh)", text)

    def test_no_budget_renders_no_parenthetical(self):
        text = self._strip_text([SubAgent(agent_type="explore", active=True)])
        self.assertNotIn("(", text)

    def test_active_dot_blinks_green_with_the_shared_phase(self):
        subs = [SubAgent(agent_type="explore", active=True)]
        old = render._BLINK_ON
        try:
            render._BLINK_ON = True
            keys_on = dict((t, k) for t, k in self._strip_keys(subs))
            render._BLINK_ON = False
            keys_off = dict((t, k) for t, k in self._strip_keys(subs))
        finally:
            render._BLINK_ON = old
        self.assertEqual(keys_on["●"], "g_work")
        self.assertEqual(keys_off["●"], "g_work_off")

    def test_completed_entry_stays_dim_check(self):
        segs = self._strip_keys([SubAgent(agent_type="explore", active=False,
                                          model="claude-opus-4-8", effort="max")])
        joined = "".join(t for t, _k in segs)
        self.assertIn("✓", joined)
        self.assertNotIn("●", joined)
        # the completed entry's model/effort keys are the dim variants
        keys = dict((t, k) for t, k in segs)
        self.assertEqual(keys["Opus 4.8"], "famd_opus")
        self.assertEqual(keys["mx"], "effd_max")

    def test_model_id_normalizes_to_display_form(self):
        self.assertEqual(render._short_model_id("claude-sonnet-5"), "Sonnet 5")
        self.assertEqual(render._short_model_id("claude-haiku-4-5-20251001"), "Haiku 4.5")
        self.assertEqual(render._short_model_id("claude-opus-4-8"), "Opus 4.8")
        # non-wire-id values pass through untouched for _clean_model to handle
        self.assertEqual(render._short_model_id("sonnet"), "sonnet")
        self.assertIsNone(render._short_model_id(None))

    def test_completed_elapsed_stops_at_ended_at_and_gains_idle_tail(self):
        """사용자 2026-07-29 '언제 끝났는지': 30m runtime that finished 45m ago must
        read `30m (45m)` — not a forever-growing 75m."""
        now = time.time()
        sa = SubAgent(agent_type="explore", active=False,
                      started_at=now - 75 * 60, ended_at=now - 45 * 60)
        self.assertEqual(render._subagent_elapsed_min(sa), 30)
        self.assertEqual(render._subagent_idle_min(sa), 45)
        text = self._strip_text([sa])
        self.assertIn("30m (45m)", text)

    def test_completed_without_ended_at_keeps_the_old_tail_only(self):
        now = time.time()
        sa = SubAgent(agent_type="explore", active=False, started_at=now - 75 * 60)
        self.assertEqual(render._subagent_elapsed_min(sa), 75)
        self.assertIsNone(render._subagent_idle_min(sa))
        self.assertNotIn("(", self._strip_text([sa]))

    def test_active_entry_never_shows_an_idle_tail(self):
        now = time.time()
        sa = SubAgent(agent_type="explore", active=True,
                      started_at=now - 10 * 60, ended_at=now - 5 * 60)
        # active rows keep counting and show no idle even with a stray ended_at
        self.assertEqual(render._subagent_elapsed_min(sa), 10)
        self.assertIsNone(render._subagent_idle_min(sa))
        self.assertNotIn("(", self._strip_text([sa]))


if __name__ == "__main__":
    unittest.main()
