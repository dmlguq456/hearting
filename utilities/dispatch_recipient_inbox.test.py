#!/usr/bin/env python3
"""Recipient inbox contracts, effect-boundary compact lineage, scope budgets.

Every state root here is an isolated temporary home: no test touches live
dispatch, session-tidy or peer state. The one live read is a read-only copy of
the sessions ledgers for the compact/full equivalence matrix.
"""
from __future__ import annotations
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])

import inspect
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "utilities"), str(ROOT / "tools")]

# Captured before any test isolates the environment: the only live input allowed.
_LIVE_STATE = Path(
    os.environ.get("HARNESS_STATE_ROOT")
    or (Path(os.environ["XDG_STATE_HOME"]) / "hearting"
        if os.environ.get("XDG_STATE_HOME") else Path.home() / ".local" / "state" / "hearting")
)

import dispatch_pending_delivery as pending  # noqa: E402
import dispatch_recipient_inbox as inbox  # noqa: E402
import dispatch_seat_handover as handover  # noqa: E402
import session_tidy as tidy  # noqa: E402
import session_tidy_clear as clear  # noqa: E402
from peer_read_context import PeerReadContext  # noqa: E402
from harness_capabilities import HARNESSES  # noqa: E402
from split_module_fixture import patch_split  # noqa: E402


class IsolatedCase(unittest.TestCase):
    """A private HOME/XDG/dispatch root per test; nothing resolves to live state."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="recipient-inbox-test-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self._saved_environ = dict(os.environ)
        self.addCleanup(self._restore_environ)
        for key in tuple(os.environ):
            if key.startswith(("AGENT_", "HERDR_", "OPENCODE_", "CLAUDE_", "CODEX_", "XDG_", "HARNESS_")):
                os.environ.pop(key, None)
        self.home = self.root / "home"
        self.home.mkdir()
        os.environ["HOME"] = str(self.home)
        os.environ["XDG_STATE_HOME"] = str(self.home / ".local" / "state")
        self.dispatch_root = self.root / "dispatch"
        self.dispatch_root.mkdir()
        self.jobs = self.dispatch_root / "jobs.log"
        self.jobs.touch()
        os.environ["AGENT_DISPATCH_JOBS"] = str(self.jobs)
        self._cache_handover()

    def _restore_environ(self):
        os.environ.clear()
        os.environ.update(self._saved_environ)

    @staticmethod
    def _cache_handover():
        handover._LINEAGE_GRAPH_CACHE.clear()

    # -- fixtures ---------------------------------------------------------
    def write_ledger(self, seat_key, rows):
        path = tidy.state_root() / "sessions" / f"{seat_key}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in rows))
        return path

    def read_ledger(self, seat_key):
        path = tidy.state_root() / "sessions" / f"{seat_key}.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

    def handover_row(self, sid, origin, bindings=(), ts=1, harness="claude", source="retire"):
        return {"ts": ts, "harness": harness, "sid": sid, "event": "handover",
                "source": source, "from": {"harness": harness, "sid": origin},
                "bindings": list(bindings)}

    def binding(self, attempt="att-1", parent="claude-A", jobs=None):
        return {"jobs": str(jobs or self.jobs), "route": "rt-fixture",
                "hash": "sha256:" + "b" * 64, "node": "execute",
                "attempt": attempt, "parent": parent}

    def meta(self, attempt="att-1", parent="claude-A"):
        return {"attempt_id": attempt, "parent_sid": parent, "parent_harness": "claude",
                "dispatch_depth": "1", "worker_type": "owner", "route_id": "rt-fixture",
                "route_hash": "sha256:" + "b" * 64, "route_node": "execute"}

    def fixture_graph(self):
        """One bound retire edge, one succession edge, activity rows, a legacy row."""
        binding = self.binding()
        rows = [
            {"ts": 10, "harness": "claude", "sid": "claude-A", "event": "start", "source": "startup"},
            {"ts": 11, "harness": "claude", "sid": "claude-A", "event": "prompt"},
            self.handover_row("claude-B", "claude-A", [binding], ts=12, source="clear"),
            {"ts": 13, "harness": "claude", "sid": "claude-B", "event": "prompt"},
        ]
        self.write_ledger("seat-bound", rows)
        self.write_ledger("seat-succession", [
            {"ts": 20, "harness": "claude", "sid": "claude-B", "event": "prompt"},
            {"ts": 21, "harness": "claude", "sid": "claude-C", "event": "succession",
             "source": "tidy-clear", "from": "claude-B"},
        ])
        legacy = {"ts": 30, "harness": "codex", "sid": "codex-B", "event": "handover",
                  "from": "codex-A", "bindings": [self.binding(attempt="att-legacy", parent="codex-A")]}
        self.write_ledger("seat-legacy", [
            {"ts": 29, "harness": "codex", "sid": "codex-A", "event": "start", "source": "startup"},
            legacy,
            {"ts": 31, "harness": "codex", "sid": "codex-B", "event": "prompt"},
        ])
        activity = [{"ts": 40 + index, "harness": "claude", "sid": "claude-noise",
                     "event": "prompt"} for index in range(120)]
        activity += [{"ts": 200 + index, "harness": "codex", "sid": f"codex-noise-{index}",
                      "event": "start", "source": "startup"} for index in range(30)]
        self.write_ledger("seat-activity", activity)
        return binding

    # -- comparisons ------------------------------------------------------
    def graphs(self):
        self._cache_handover()
        full = handover.lineage_graph(read_context=PeerReadContext())
        compact = handover.lineage_graph(read_context=PeerReadContext(compact_lineage=True))
        return full, compact

    def assert_equivalent(self, full, compact):
        self.assertEqual(full.edges, compact.edges)
        self.assertEqual(full.unresolved, compact.unresolved)
        self.assertEqual(dict(full.ledger_keys), dict(compact.ledger_keys))
        self.assertEqual({key: dict(value) for key, value in full.binding_rows.items()},
                         {key: dict(value) for key, value in compact.binding_rows.items()})
        self.assertEqual(full.seat_edges, compact.seat_edges)
        self.assertEqual(full.seat_unresolved, compact.seat_unresolved)
        self.assertEqual(full.binding_conflicts, compact.binding_conflicts)
        self.assertEqual(set(full.ledger_rows), set(compact.ledger_rows))
        # Compaction may drop only rows no resolver reads: every kept row survives
        # in order, and a compact view is never larger than the full one.
        for key, rows in full.ledger_rows.items():
            keep = handover._compact_row_indexes(rows)
            expected = [dict(rows[index]) for index in sorted(keep)]
            actual = [dict(row) for row in compact.ledger_rows[key]]
            self.assertLessEqual(len(actual), len(rows))
            position = 0
            for item in expected:
                while position < len(actual) and actual[position] != item:
                    position += 1
                self.assertLess(position, len(actual), f"{key}: missing resolver row {item!r}")
                position += 1
        # Observation identity: same files and generations, minus the byte digests
        # the compact path never reads.
        self.assertEqual(full.generation[0], compact.generation[0])
        self.assertEqual([tuple(item)[:7] for item in full.generation[1]],
                         [tuple(item) for item in compact.generation[1]])
        self.assertEqual(full.generation[2], compact.generation[2])

    def resolver_answers(self, kind):
        context = PeerReadContext(compact_lineage=(kind == "compact"))
        answers = {
            "chain_end": handover.chain_end("claude", "claude-A", read_context=context),
            "succession_end": handover.chain_end("claude", "claude-B", read_context=context),
            "seat_successor": tuple(
                item for item in handover.resolve_recipient(
                    "seat-successor", session="claude-A", harness="claude",
                    read_context=context)[:3]),
            "effective_parent": handover.effective_parent(
                self.meta(), self.jobs, read_context=context),
            "binding": handover.record_recipient_binding(
                {"attempt_ids": ["att-1"]}, "claude-A", harness="claude",
                jobs=self.jobs, read_context=context),
            "legacy_binding": handover.record_recipient_binding(
                {"attempt_ids": ["att-legacy"]}, "codex-A", harness="codex",
                jobs=self.jobs, read_context=context),
            "storage": [(key, tuple(sorted(attempts)) if attempts is not None else None)
                        for key, attempts in handover.storage_recipients(
                            "claude-B", harness="claude", read_context=context)],
            "legacy_storage": [(key, tuple(sorted(attempts)) if attempts is not None else None)
                               for key, attempts in handover.storage_recipients(
                                   "codex-B", harness="codex", read_context=context)],
        }
        return answers

    def ledger_open_count(self, *, compact):
        reads = []
        real = tidy.read_bytes

        def read(path):
            if Path(path).suffix == ".jsonl":
                reads.append(Path(path))
            return real(path)

        self._cache_handover()
        with mock.patch.object(tidy, "read_bytes", side_effect=read):
            graph = handover.lineage_graph(
                read_context=PeerReadContext(compact_lineage=compact))
        return graph, reads

    def graph_cost(self, *, compact):
        measured = {"files": 0, "bytes": 0}
        real = tidy.read_bytes

        def read(path):
            raw = real(path)
            if Path(path).suffix == ".jsonl" and raw is not None:
                measured["files"] += 1
                measured["bytes"] += len(raw)
            return raw

        self._cache_handover()
        started = time.monotonic()
        with mock.patch.object(tidy, "read_bytes", side_effect=read):
            graph = handover.lineage_graph(
                read_context=PeerReadContext(compact_lineage=compact))
        measured["seconds"] = round(time.monotonic() - started, 4)
        return graph, measured


class InboxContractTest(IsolatedCase):

    def inbox_payload(self, *, delivery_id="delivery-inbox-1", storage_key="claude-A"):
        record = pending.record_path(self.dispatch_root, storage_key, delivery_id)
        record.parent.mkdir(parents=True, exist_ok=True)
        record.write_text("{}\n")
        return {
            "schema": inbox.INBOX_SCHEMA_VERSION,
            "edge_generation": inbox.read_edge_generation(self.dispatch_root),
            "folded_edge_digest": inbox.edge_digest([("claude", "A", "B")]),
            "dir_identities": self.scope_identities(storage_key),
            "delivery_ids": [{"storage_key": storage_key, "delivery_id": delivery_id,
                              "mtime_ns": record.stat().st_mtime_ns}],
            "candidates": [{"storage_key": storage_key, "attempts": ["att-1"]}],
            "observed_at_ns": time.time_ns(),
        }

    def scope_identities(self, storage_key):
        path = pending.record_directory(self.dispatch_root, storage_key)
        info = path.stat()
        return {str(path): [info.st_dev, info.st_ino, info.st_mtime_ns]}

    def test_canonical_root_is_the_dispatch_root_not_session_tidy(self):
        self.assertEqual(inbox.dispatch_inbox_root(), self.dispatch_root)
        self.assertEqual(tidy.state_root(), self.home / ".local" / "state" / "hearting" / "session-tidy")
        self.assertNotEqual(inbox.dispatch_inbox_root(), tidy.state_root())
        # Record-state change moves the inbox root and not the session-tidy root.
        other = self.root / "other-dispatch"
        with mock.patch.dict(os.environ, {"AGENT_DISPATCH_JOBS": str(other / "jobs.log")}):
            self.assertEqual(inbox.dispatch_inbox_root(), other)
            self.assertEqual(tidy.state_root(), self.home / ".local" / "state" / "hearting" / "session-tidy")
        # The explicit registry a caller already holds wins over ambient state.
        self.assertEqual(inbox.dispatch_inbox_root(jobs=self.jobs), self.dispatch_root)
        self.assertEqual(
            inbox.dispatch_inbox_root(environ={"HOME": str(self.home)}),
            self.home / ".local" / "state" / "hearting" / "dispatch")
        self.assertEqual(
            inbox.dispatch_inbox_root(environ={"HARNESS_STATE_ROOT": str(self.root / "harness")}),
            self.root / "harness" / "dispatch")

    def test_paths_are_private_and_traversal_is_refused(self):
        self.assertEqual(inbox.inbox_path(self.dispatch_root, "claude", "sid-1"),
                         self.dispatch_root / "recipient-inbox" / "claude" / "sid-1.json")
        self.assertEqual(inbox.edge_generation_path(self.dispatch_root),
                         self.dispatch_root / "recipient-inbox" / "edge-generation.json")
        self.assertEqual(inbox.lineage_rows_path(self.dispatch_root),
                         self.dispatch_root / "recipient-inbox" / "lineage-rows.json")
        self.assertEqual(inbox.run_states_path(self.dispatch_root),
                         self.dispatch_root / "recipient-inbox" / "run-states.json")
        for harness, sid in (("claude", "../escape"), ("../claude", "sid"), ("claude", "a/b"),
                             ("claude", ""), ("", "sid")):
            with self.subTest(harness=harness, sid=sid), self.assertRaises(ValueError):
                inbox.inbox_path(self.dispatch_root, harness, sid)
        self.assertIsNone(inbox.read_inbox(self.dispatch_root, "claude", "../escape"))

    def test_inbox_write_read_is_atomic_and_private(self):
        payload = self.inbox_payload()
        inbox.write_inbox(self.dispatch_root, "claude", "sid-1", payload)
        path = inbox.inbox_path(self.dispatch_root, "claude", "sid-1")
        recorded = inbox.read_inbox(self.dispatch_root, "claude", "sid-1")
        self.assertEqual(recorded["schema"], inbox.INBOX_SCHEMA_VERSION)
        self.assertEqual(recorded["delivery_ids"], payload["delivery_ids"])
        self.assertEqual(recorded["dir_identities"], payload["dir_identities"])
        self.assertEqual(recorded["candidates"], payload["candidates"])
        self.assertEqual(recorded["folded_edge_digest"], payload["folded_edge_digest"])
        self.assertIsInstance(recorded["observed_at_ns"], int)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual([name for name in os.listdir(path.parent) if name.endswith(".tmp")], [])

    def test_missing_corrupt_and_foreign_inboxes_read_as_none(self):
        self.assertIsNone(inbox.read_inbox(self.dispatch_root, "claude", "absent"))
        path = inbox.inbox_path(self.dispatch_root, "claude", "broken")
        path.parent.mkdir(parents=True)
        for raw in (b"{not-json", b"[]", b"null",
                    json.dumps({"schema": inbox.INBOX_SCHEMA_VERSION + 1}).encode(),
                    json.dumps({"schema": inbox.INBOX_SCHEMA_VERSION,
                                "edge_generation": 0, "folded_edge_digest": None,
                                "dir_identities": {}, "delivery_ids": [{"storage_key": "a"}],
                                "candidates": [], "observed_at_ns": 1}).encode()):
            path.write_bytes(raw)
            with self.subTest(raw=raw[:24]):
                self.assertIsNone(inbox.read_inbox(self.dispatch_root, "claude", "broken"))
        valid = {"schema": inbox.INBOX_SCHEMA_VERSION, "edge_generation": 0,
                 "folded_edge_digest": None, "dir_identities": {}, "delivery_ids": [],
                 "candidates": [], "observed_at_ns": 1}
        path.write_text(json.dumps(valid))
        self.assertEqual(inbox.read_inbox(self.dispatch_root, "claude", "broken"), valid)
        with self.assertRaises(ValueError):
            inbox.write_inbox(self.dispatch_root, "claude", "bad",
                              {"edge_generation": "x", "dir_identities": {}, "delivery_ids": [],
                               "candidates": [], "folded_edge_digest": None, "observed_at_ns": 1})
        symlink = inbox.inbox_path(self.dispatch_root, "claude", "linked")
        symlink.parent.mkdir(parents=True, exist_ok=True)
        symlink.symlink_to(path)
        self.assertIsNone(inbox.read_inbox(self.dispatch_root, "claude", "linked"))

    def test_edge_generation_is_monotonic_and_fail_open(self):
        self.assertEqual(inbox.read_edge_generation(self.dispatch_root), 0)
        self.assertEqual(inbox.bump_edge_generation(self.dispatch_root), 1)
        self.assertEqual(inbox.bump_edge_generation(self.dispatch_root), 2)
        self.assertEqual(inbox.read_edge_generation(self.dispatch_root), 2)
        inbox.edge_generation_path(self.dispatch_root).write_text("{")
        self.assertEqual(inbox.read_edge_generation(self.dispatch_root), 0)
        self.assertEqual(inbox.bump_edge_generation(self.dispatch_root), 1)

    def test_is_current_covers_edges_named_records_and_scope_directories(self):
        payload = self.inbox_payload()
        digest = payload["folded_edge_digest"]
        self.assertTrue(inbox.is_current(payload, self.dispatch_root, folded_edge_digest=digest))
        # A digest mismatch only matters when the caller observed one.
        self.assertFalse(inbox.is_current(payload, self.dispatch_root, folded_edge_digest="other"))
        self.assertTrue(inbox.is_current(payload, self.dispatch_root))
        # A named open record rewritten or removed makes the discovery stale.
        record = pending.record_path(self.dispatch_root, "claude-A", "delivery-inbox-1")
        record.write_text('{"changed": true}\n')
        # Fast writes can share the filesystem's timestamp tick. This check
        # exercises a changed metadata generation, not wall-clock resolution.
        changed = record.stat().st_mtime_ns + 1_000_000
        os.utime(record, ns=(changed, changed))
        self.assertFalse(inbox.is_current(payload, self.dispatch_root))
        record.unlink()
        self.assertFalse(inbox.is_current(payload, self.dispatch_root))
        # A record appearing in a named scope directory changes its identity.
        record.write_text("{}\n")
        payload = self.inbox_payload()
        self.assertTrue(inbox.is_current(payload, self.dispatch_root, folded_edge_digest=digest))
        directory = pending.record_directory(self.dispatch_root, "claude-A")
        (directory / "delivery-new.json").write_text("{}\n")
        changed = directory.stat().st_mtime_ns + 1_000_000
        os.utime(directory, ns=(changed, changed))
        self.assertFalse(inbox.is_current(payload, self.dispatch_root))
        # A new edge generation invalidates every derived inbox.
        payload = self.inbox_payload()
        inbox.bump_edge_generation(self.dispatch_root)
        self.assertFalse(inbox.is_current(payload, self.dispatch_root))
        payload = self.inbox_payload()
        self.assertTrue(inbox.is_current(payload, self.dispatch_root))

    def test_is_current_requires_an_absent_scope_to_stay_absent(self):
        payload = self.inbox_payload()
        missing = pending.record_directory(self.dispatch_root, "claude-absent")
        payload["dir_identities"][str(missing)] = None
        self.assertFalse(missing.exists())
        self.assertTrue(inbox.is_current(payload, self.dispatch_root))
        missing.mkdir(parents=True)
        self.assertFalse(inbox.is_current(payload, self.dispatch_root))

    def test_folded_edge_digest_tracks_the_remaining_edge_set(self):
        one = inbox.edge_digest([("claude", "A", "B")])
        self.assertEqual(one, inbox.edge_digest([("claude", "A", "B")]))
        self.assertNotEqual(one, inbox.edge_digest([("claude", "A", "B"), ("claude", "B", "C")]))
        self.assertNotEqual(one, inbox.edge_digest([]))

    def test_pending_record_path_matches_the_delivery_reader(self):
        for storage_key, delivery_id in (("claude-A", "delivery-abc"),
                                         ("sha256:" + "c" * 8, "delivery-" + "d" * 8)):
            with self.subTest(storage_key=storage_key):
                self.assertEqual(inbox.pending_record_path(self.dispatch_root, storage_key, delivery_id),
                                 pending.record_path(self.dispatch_root, storage_key, delivery_id))

    def test_run_states_fail_open_and_publish_atomically(self):
        self.assertEqual(inbox.read_run_states(self.dispatch_root), {})
        inbox.write_run_states(self.dispatch_root, {"runs": {"run-1": {
            "status": "done", "exit_code": 0, "ended_at": 1.5,
            "artifact_root": "/artifacts", "parent_attempt_id": "att-1",
            "jobs": str(self.jobs)}}})
        payload = inbox.read_run_states(self.dispatch_root)
        self.assertEqual(payload["schema"], inbox.RUN_STATES_SCHEMA)
        self.assertEqual(payload["runs"]["run-1"]["status"], "done")
        self.assertEqual(payload["runs"]["run-1"]["exit_code"], 0)
        path = inbox.run_states_path(self.dispatch_root)
        for raw in ("{", "[]", json.dumps({"schema": 99, "runs": {}}),
                    json.dumps({"schema": inbox.RUN_STATES_SCHEMA, "runs": []})):
            path.write_text(raw)
            self.assertEqual(inbox.read_run_states(self.dispatch_root), {})

    def test_steward_relations_fail_open_and_publish_atomically(self):
        self.assertIsNone(inbox.read_steward_relations(self.dispatch_root))
        inbox.write_steward_relations(self.dispatch_root, [
            {"holder": {"harness": "claude", "sid": "s"}, "released": False}])
        self.assertEqual(inbox.read_steward_relations(self.dispatch_root)[0]["holder"]["sid"], "s")
        path = inbox.steward_relations_path(self.dispatch_root)
        for raw in ("{", "[]", json.dumps({"schema": 99, "relations": []}),
                    json.dumps({"schema": inbox.STEWARD_RELATIONS_SCHEMA,
                                "relations": [1]})):
            path.write_text(raw)
            self.assertIsNone(inbox.read_steward_relations(self.dispatch_root))
        with self.assertRaises(ValueError):
            inbox.write_steward_relations(self.dispatch_root, [1])

    def test_lineage_rows_fail_open_and_refuse_unusable_payloads(self):
        self.assertIsNone(inbox.read_lineage_rows(self.dispatch_root))
        inbox.write_lineage_rows(self.dispatch_root, {
            "directory": "/sessions", "ledgers": {"k": {"generation": [1, 2, 3, 4, 5, 6],
                                                        "rows": [[0, {"sid": "s"}]]}}})
        payload = inbox.read_lineage_rows(self.dispatch_root)
        self.assertEqual(payload["schema"], inbox.LINEAGE_ROWS_SCHEMA)
        self.assertIn("k", payload["ledgers"])
        inbox.lineage_rows_path(self.dispatch_root).write_text(
            json.dumps({"schema": inbox.LINEAGE_ROWS_SCHEMA, "ledgers": []}))
        self.assertIsNone(inbox.read_lineage_rows(self.dispatch_root))
        with self.assertRaises(ValueError):
            inbox.write_lineage_rows(self.dispatch_root, {"ledgers": []})
        with self.assertRaises(ValueError):
            inbox.write_lineage_rows(self.dispatch_root, {"directory": "/sessions", "ledgers": []})
        with self.assertRaises(ValueError):
            inbox.write_lineage_rows(self.dispatch_root, {"ledgers": {}})


class ScopeBudgetTest(IsolatedCase):

    def test_storage_recipients_reports_completion_and_keeps_its_pair_shape(self):
        self.write_ledger("seat-budget", [
            {"ts": 1, "harness": "claude", "sid": "claude-A", "event": "start", "source": "startup"},
            self.handover_row("claude-B", "claude-A", [self.binding()], ts=2, source="clear"),
        ])
        full = handover.storage_recipients("claude-B", harness="claude")
        self.assertTrue(full.complete)
        self.assertIsInstance(full, list)
        self.assertEqual([key for key, _attempts in full], ["claude-B", "claude-A"])
        self.assertEqual(dict(full)["claude-B"], None)
        self.assertIsInstance(dict(full)["claude-A"], frozenset)
        # Existing consumers keep iterating pairs; the budget only adds a flag.
        for storage_key, allowed in full:
            self.assertIsInstance(storage_key, str)
        expired = handover.storage_recipients("claude-B", harness="claude",
                                              deadline_ns=time.monotonic_ns() - 1)
        self.assertFalse(expired.complete)
        self.assertEqual([key for key, _attempts in expired], ["claude-B"])
        # The graph read is not started at all once the caller's budget is gone.
        with mock.patch.object(handover, "_pass_lineage_graph",
                               side_effect=AssertionError("deadline must precede the graph")):
            late = handover.storage_recipients("claude-B", harness="claude",
                                               deadline_ns=time.monotonic_ns() - 1)
        self.assertFalse(late.complete)
        # Unreadable authority is not an empty, complete enumeration.
        with mock.patch.object(handover, "_pass_lineage_graph", side_effect=OSError("fixture")):
            broken = handover.storage_recipients("claude-B", harness="claude")
        self.assertFalse(broken.complete)
        self.assertEqual(list(broken), [])

    def test_compact_context_is_opt_in_only(self):
        self.fixture_graph()
        self.assertFalse(PeerReadContext().compact_lineage)
        self.assertFalse(PeerReadContext(registry_append_only=True).compact_lineage)
        self.assertTrue(PeerReadContext(compact_lineage=True).compact_lineage)
        # The runner, Fleet readers and the courier keep their full observations;
        # only the sweep's effect groups opt in, and that file is not pinned here.
        steward_parts = [f"utilities/{path.name}"
                         for path in sorted((ROOT / "utilities").glob("peer_steward_*.py"))
                         if "Split out of `peer-steward.py`" in path.read_text(encoding="utf-8")]
        for relative in ("utilities/peer-steward.py", *steward_parts,
                         "tools/fleet/collectors/dispatch.py",
                         "tools/fleet/collectors/__init__.py",
                         "utilities/dispatch_lineage_observation.test.py"):
            text = (ROOT / relative).read_text(encoding="utf-8")
            with self.subTest(relative=relative):
                self.assertNotIn("compact_lineage=True", text)


class EdgeGenerationWriterTest(IsolatedCase):

    def test_every_confirmed_edge_writer_bumps_and_activity_rows_do_not(self):
        self.assertIn("_bump_dispatch_edge_generation", inspect.getsource(handover.record_retire_handover))
        self.assertIn("_bump_dispatch_edge_generation", inspect.getsource(handover._record_locked))
        self.assertIn("bump_dispatch_edge_generation", inspect.getsource(tidy._fold_ledger))
        # Prompt and real-prompt activity rows are not edges: no bump there.
        self.assertNotIn("bump_dispatch_edge_generation", inspect.getsource(tidy._append_ledger_entry))
        self.assertNotIn("bump_dispatch_edge_generation", inspect.getsource(tidy.record_real_prompt))

    def test_retire_handover_bumps(self):
        seat = tidy.Seat("pane", "retire-seat", pane="w1:pRetire", harness="claude")
        before = inbox.read_edge_generation(self.dispatch_root)
        row = handover.record_retire_handover("retire-old", "claude", "retire-new", "claude",
                                              successor_seat=seat)
        self.assertIsNotNone(row)
        self.assertEqual((handover._st()._handover_origin(row), row["sid"]),
                         (("claude", "retire-old"), "retire-new"))
        self.assertEqual(inbox.read_edge_generation(self.dispatch_root), before + 1)

    def test_confirmed_clear_handover_bumps(self):
        seat = tidy.Seat("pane", "clear-seat", pane="w1:pClear", harness="claude")
        now = tidy.now_epoch()
        tidy.atomic_write_json(handover._snapshot_path(seat.key), {
            "schema": handover.SCHEMA, "seat": seat.as_dict(),
            "from": {"harness": "claude", "sid": "clear-old"}, "at": now - 1,
            "bindings": []})
        tidy._append_ledger_entry(seat, {"ts": now, "harness": "claude", "sid": "clear-new",
                                         "event": "start", "source": "clear"})
        before = inbox.read_edge_generation(self.dispatch_root)
        row = handover.record_locked(seat, "claude", "clear-new", "start", "clear", now)
        self.assertIsNotNone(row)
        self.assertEqual((handover._st()._handover_origin(row), row["sid"]),
                         (("claude", "clear-old"), "clear-new"))
        self.assertEqual(inbox.read_edge_generation(self.dispatch_root), before + 1)

    def test_fold_that_drops_edges_bumps(self):
        seat = tidy.Seat("pane", "fold-seat")
        rows = [self.handover_row(f"fold-{index}", f"prev-{index}", ts=index + 1)
                for index in range(60)]
        rows += [{"ts": 1000 + index, "harness": "claude", "sid": "fold-noise",
                  "event": "prompt"} for index in range(600)]
        self.write_ledger("fold-seat", rows)
        before = inbox.read_edge_generation(self.dispatch_root)
        tidy._fold_ledger(seat)
        folded = self.read_ledger("fold-seat")
        self.assertEqual(len([row for row in folded if row.get("event") == "handover"]), 40)
        self.assertEqual(inbox.read_edge_generation(self.dispatch_root), before + 1)
        # A fold that keeps every edge is not an edge change.
        tidy._fold_ledger(seat)
        self.assertEqual(inbox.read_edge_generation(self.dispatch_root), before + 1)


class CompactLineageEquivalenceTest(IsolatedCase):

    def publish(self):
        return handover.publish_lineage_rows(self.dispatch_root)

    def test_changed_compact_ledger_retains_authority_without_activity_history(self):
        self.fixture_graph()
        self.publish()
        path = tidy.state_root() / "sessions" / "seat-bound.jsonl"
        with path.open("a") as stream:
            for index in range(200):
                stream.write(json.dumps({"event": "prompt", "harness": "claude",
                    "sid": "claude-B", "ts": index + 100}) + "\n")
        complete, compact = self.graphs()
        self.assert_equivalent(complete, compact)
        self.assertLess(len(compact.ledger_rows["seat-bound"]),
                        len(complete.ledger_rows["seat-bound"]))
        self.assertEqual(self.resolver_answers("full"), self.resolver_answers("compact"))

    def test_fork_bindings_hidden_fork_and_unreadable_recovery_match_full_projection(self):
        rows = [dict(event="succession", source="tidy-clear", harness="claude", sid=target,
                     ts=1, **{"from": origin})
                for origin, target in (("P", "A"), ("A", "B"), ("B", "C"), ("A", "D"))]
        binding = self.binding(parent="A")
        rows.append(self.handover_row("B", "A", [binding], source="clear", ts=2))
        path = self.write_ledger("fork", rows)
        self.publish()
        contexts = [PeerReadContext(), PeerReadContext(compact_lineage=True)]
        def verify(expected):
            full, compact = self.graphs()
            self.assert_equivalent(full, compact)
            self.assertEqual([handover.effective_parent(self.meta(parent="A"), self.jobs, read_context=ctx)
                              for ctx in contexts], [expected, expected])
            self.assertEqual(handover.chain_end("claude", "C"), ("C", "self"))
        verify("C")
        # A competing claim at a different depth must survive compact publication,
        # physical duplicates, file order, and a warm reader's earlier observation.
        rows.append(self.handover_row("E", "D", [binding], source="clear", ts=20))
        for reverse in (False, True):
            self.write_ledger("fork", list(reversed(rows)) if reverse else rows)
            self.write_ledger("duplicate", [rows[-1]])
            verify("")
            self.publish()
            verify("")
        original = path.read_bytes()
        path.write_bytes(original + b"{corrupt\n")
        for ctx in contexts:
            self.assertEqual(handover.effective_parent(self.meta(parent="A"), self.jobs, read_context=ctx), "")
            with self.assertRaises(OSError):
                handover.lineage_graph(read_context=ctx)
        path.write_bytes(original)
        verify("")
        # A second fork hidden in a new ledger invalidates C but leaves full and
        # compact readers in agreement after their warm observations.
        self.write_ledger("hidden", [self.handover_row("F", "C", source="clear"),
                                     self.handover_row("G", "C", source="clear")])
        self.publish()
        self.assert_equivalent(*self.graphs())
        self.assertEqual(handover.chain_end("claude", "C"), ("C", "unresolved"))

    def test_old_scope_inbox_is_normally_refreshed_and_ancestor_notice_is_not_deliverable(self):
        import importlib.util
        import dispatch_session_sweep as sweep
        import peer_obligations
        import session_notice
        spec = importlib.util.spec_from_file_location("inbox_fork_steward", ROOT / "utilities/peer-steward.py")
        steward = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = steward
        spec.loader.exec_module(steward)
        self.addCleanup(sys.modules.pop, spec.name, None)
        rows = [dict(event="succession", source="tidy-clear", harness="claude", sid=target,
                     ts=1, **{"from": origin})
                for origin, target in (("P", "A"), ("A", "B"), ("B", "C"), ("A", "D"))]
        self.write_ledger("fork", rows)
        with mock.patch.object(peer_obligations, "ensure_normal_runner", return_value=False), \
             mock.patch.object(peer_obligations, "request_dispatch_reconciliation", return_value=False):
            records = [session_notice.notify("claude", sid, key="inbox-" + sid,
                subject="fixture", text="pending result", jobs=self.jobs) for sid in ("A", "B")]
        graph = handover.lineage_graph()
        old = dict(schema=1, edge_generation=inbox.read_edge_generation(self.dispatch_root),
            folded_edge_digest=inbox.edge_digest(graph.edges), dir_identities={}, delivery_ids=[],
            candidates=[dict(storage_key="C", attempts=None)], observed_at_ns=time.time_ns())
        path = inbox.inbox_path(self.dispatch_root, "claude", "C")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(old))
        with patch_split(steward, "_live_recipients", return_value={("claude", "C")}):
            result = steward._prefill_live_inboxes(self.dispatch_root, deadline_ns=time.monotonic_ns() + 10**10)
        self.assertEqual(result["written"], 1)
        refreshed = inbox.read_inbox(self.dispatch_root, "claude", "C")
        self.assertEqual(refreshed["schema"], inbox.INBOX_SCHEMA_VERSION)
        self.assertEqual({entry["storage_key"] for entry in refreshed["candidates"]}, {"B", "C"})
        addressed, _ = sweep.addressed_records(self.dispatch_root, "claude-parent-runtime", "C")
        self.assertEqual([record["delivery_id"] for record in addressed], [records[1]["delivery_id"]])
        # The new projection is only discovery. A later fork changes the live
        # per-record answer even if a caller still holds its cached scope.
        self.write_ledger("later", [self.handover_row("F", "C", source="clear"),
                                    self.handover_row("G", "C", source="clear")])
        self.assertFalse(handover.record_for_session(records[1], None, session="C", harness="claude",
                                                    storage_key="B", jobs=self.jobs))

    def test_compact_and_full_observations_agree_through_every_mutation(self):
        self.fixture_graph()
        published = self.publish()
        self.assertGreaterEqual(published["ledgers"], 4)
        self.assertEqual(published["skipped"], 0)

        full, compact = self.graphs()
        self.assert_equivalent(full, compact)
        self.assertEqual(self.resolver_answers("full"), self.resolver_answers("compact"))
        self.assertEqual(len(compact.edges), 2)

        # Fresh payload: the compact effect observation opens no ledger byte.
        graph, compact_reads = self.ledger_open_count(compact=True)
        self.assertEqual(compact_reads, [])
        graph, full_reads = self.ledger_open_count(compact=False)
        self.assertEqual(len(full_reads), 4)

        # Append without republishing: only the changed ledger is reopened.
        path = tidy.state_root() / "sessions" / "seat-bound.jsonl"
        with path.open("a") as stream:
            stream.write(json.dumps(self.handover_row("claude-C", "claude-B", ts=50,
                                                      source="clear")) + "\n")
        graph, reads = self.ledger_open_count(compact=True)
        self.assertEqual(reads, [path])
        full, compact = self.graphs()
        self.assert_equivalent(full, compact)
        self.assertIn(("claude", "claude-B", "claude-C"), compact.edges)
        self.assertEqual(self.resolver_answers("full"), self.resolver_answers("compact"))

        # Republish: the same observation is stat-only again.
        self.publish()
        graph, reads = self.ledger_open_count(compact=True)
        self.assertEqual(reads, [])

        # Fold: edges beyond the retained window are gone, and both paths agree.
        self.write_ledger("seat-bound", self.read_ledger("seat-bound") + [
            {"ts": 60 + index, "harness": "claude", "sid": "claude-noise",
             "event": "prompt"} for index in range(600)])
        tidy._fold_ledger(tidy.Seat("pane", "seat-bound"))
        self.publish()
        full, compact = self.graphs()
        self.assert_equivalent(full, compact)
        self.assertEqual(self.resolver_answers("full"), self.resolver_answers("compact"))

        # chmod: a changed generation is reopened and fails closed like the full read.
        target = tidy.state_root() / "sessions" / "seat-succession.jsonl"
        mode = target.stat().st_mode & 0o777
        target.chmod(0)
        try:
            with self.assertRaises(OSError):
                handover.lineage_graph(read_context=PeerReadContext(compact_lineage=True))
            with self.assertRaises(OSError):
                handover.lineage_graph(read_context=PeerReadContext())
        finally:
            target.chmod(mode)
        full, compact = self.graphs()
        self.assert_equivalent(full, compact)

        # Removal: a stale payload entry never resurrects a ledger.
        removed = tidy.state_root() / "sessions" / "seat-activity.jsonl"
        removed.unlink()
        full, compact = self.graphs()
        self.assert_equivalent(full, compact)
        self.assertNotIn("seat-activity", compact.ledger_rows)
        self.assertIn("seat-activity", inbox.read_lineage_rows(self.dispatch_root)["ledgers"])
        self.publish()
        self.assertNotIn("seat-activity", inbox.read_lineage_rows(self.dispatch_root)["ledgers"])
        full, compact = self.graphs()
        self.assert_equivalent(full, compact)

    def test_missing_or_corrupt_rows_fall_back_to_the_full_read(self):
        self.fixture_graph()
        full, _compact = self.graphs()
        self.assertEqual(len(self.ledger_open_count(compact=True)[1]), 4)
        inbox.lineage_rows_path(self.dispatch_root).parent.mkdir(parents=True, exist_ok=True)
        inbox.lineage_rows_path(self.dispatch_root).write_text("{")
        self.assertEqual(len(self.ledger_open_count(compact=True)[1]), 4)
        payload = self.publish()
        self.assertIsNotNone(payload)
        # A payload from another state directory is never trusted.
        rows = inbox.read_lineage_rows(self.dispatch_root)
        rows["directory"] = "/elsewhere"
        inbox.write_lineage_rows(self.dispatch_root, rows)
        self.assertEqual(len(self.ledger_open_count(compact=True)[1]), 4)
        # A malformed row entry for one ledger re-reads only that ledger.
        self.publish()
        rows = inbox.read_lineage_rows(self.dispatch_root)
        rows["ledgers"]["seat-bound"]["rows"] = [["bad"]]
        inbox.write_lineage_rows(self.dispatch_root, rows)
        self.assertEqual(len(self.ledger_open_count(compact=True)[1]), 1)

    def test_unchanged_ledger_read_fault_does_not_block_delivery(self):
        """Approved notice 3: the generation tuple proves the bytes already parsed."""
        self.fixture_graph()
        self.publish()
        path = tidy.state_root() / "sessions" / "seat-bound.jsonl"
        real = tidy.read_bytes

        def read(candidate):
            return None if Path(candidate) == path else real(candidate)

        with mock.patch.object(tidy, "read_bytes", side_effect=read):
            compact = handover.lineage_graph(read_context=PeerReadContext(compact_lineage=True))
            self.assertIn(("claude", "claude-A", "claude-B"), compact.edges)
            # Dropping the derived file restores the fail-closed full read.
            inbox.lineage_rows_path(self.dispatch_root).unlink()
            with self.assertRaises(OSError):
                handover.lineage_graph(read_context=PeerReadContext(compact_lineage=True))

    def test_lineage_summary_readers_keep_every_row(self):
        self.fixture_graph()
        self.publish()
        seat = tidy.Seat("pane", "seat-bound")
        seen = []
        real = tidy._lineage_history_index

        def history(ledgers):
            seen.append(sum(len(rows) for rows in ledgers.values()))
            return real(ledgers)

        with mock.patch.object(tidy, "_lineage_history_index", side_effect=history):
            tidy.seat_lineage(seat)
            tidy.seat_lineage(seat, read_context=PeerReadContext(compact_lineage=True))
        full_rows = sum(len(rows) for rows in handover.lineage_graph(
            read_context=PeerReadContext()).ledger_rows.values())
        self.assertEqual(seen, [full_rows, full_rows])
        # Activity-only rows are exactly what a compact view drops.
        compact_rows = sum(len(rows) for rows in handover.lineage_graph(
            read_context=PeerReadContext(compact_lineage=True)).ledger_rows.values())
        self.assertLess(compact_rows, full_rows)

    def test_compact_matches_full_on_a_read_only_live_copy(self):
        source = _LIVE_STATE / "session-tidy" / "sessions"
        if not source.is_dir():
            print(json.dumps({"live_copy": "unavailable", "source": str(source)}))
            return
        files = sorted(path for path in source.glob("*.jsonl") if path.is_file())
        if not files:
            print(json.dumps({"live_copy": "empty", "source": str(source)}))
            return
        destination = tidy.state_root() / "sessions"
        destination.mkdir(parents=True, exist_ok=True)
        copied, total = 0, 0
        for path in files[:600]:
            size = path.stat().st_size
            if total + size > 40 * 1024 * 1024:
                break
            shutil.copyfile(path, destination / path.name)
            copied += 1
            total += size
        published = self.publish()
        self.assertEqual(published["ledgers"] + published["skipped"], copied)
        full, compact = self.graphs()
        self.assert_equivalent(full, compact)
        self.assertEqual(len(compact.edges), len(full.edges))
        # A sample of real session answers must agree through both paths.
        vertices = sorted({vertex for vertex, _keys in full.ledger_keys.items()
                           if vertex[0] in HARNESSES})[:20]
        full_context, compact_context = PeerReadContext(), PeerReadContext(compact_lineage=True)
        full_answers = [(handover.chain_end(harness, sid, read_context=full_context),
                         tuple(handover.resolve_recipient(
                             "seat-successor", session=sid, harness=harness,
                             read_context=full_context)[:3]))
                        for harness, sid in vertices]
        compact_answers = [(handover.chain_end(harness, sid, read_context=compact_context),
                            tuple(handover.resolve_recipient(
                                "seat-successor", session=sid, harness=harness,
                                read_context=compact_context)[:3]))
                           for harness, sid in vertices]
        self.assertEqual(full_answers, compact_answers)
        graph, reads = self.ledger_open_count(compact=True)
        self.assertEqual(reads, [])
        _, full_cost = self.graph_cost(compact=False)
        _, compact_cost = self.graph_cost(compact=True)
        print(json.dumps({"live_copy": str(source), "ledgers": copied,
                          "bytes": total, "skipped": len(files) - copied,
                          "edges": len(full.edges), "vertices": len(vertices),
                          "full_rows": sum(len(rows) for rows in full.ledger_rows.values()),
                          "compact_rows": sum(len(rows) for rows in graph.ledger_rows.values()),
                          "full_observation": full_cost, "compact_observation": compact_cost}))


class StewardDisplayPathTest(IsolatedCase):
    """The Stop header reuses the runner observation; authority keeps the live read."""

    def test_published_relation_is_used_and_a_missing_file_falls_back_once(self):
        from fleet import herdr_projection
        from fleet.collectors import steward as collector

        markers = {("claude", "s1"): {"session_id": "s1"}}
        inbox.write_steward_relations(self.dispatch_root, [])
        with mock.patch.object(collector, "read_markers", return_value=markers), \
                mock.patch.object(collector, "read_relations",
                                  side_effect=AssertionError("live relation read")), \
                mock.patch.object(collector, "self_role_candidate", return_value=False):
            self.assertFalse(herdr_projection.is_steward(
                "claude", "s1", published_only=True))

        inbox.steward_relations_path(self.dispatch_root).unlink()
        calls = []
        with mock.patch.object(collector, "read_markers", return_value=markers), \
                mock.patch.object(collector, "read_relations",
                                  side_effect=lambda: calls.append(1) or []), \
                mock.patch.object(collector, "self_role_candidate", return_value=False):
            self.assertFalse(herdr_projection.is_steward(
                "claude", "s1", published_only=True))
        self.assertEqual(calls, [1])

    def test_no_markers_returns_before_any_relation_read(self):
        from fleet import herdr_projection
        from fleet.collectors import steward as collector

        with mock.patch.object(collector, "read_markers", return_value={}), \
                mock.patch.object(collector, "read_relations",
                                  side_effect=AssertionError("live relation read")):
            self.assertFalse(herdr_projection.is_steward(
                "claude", "s1", published_only=True))


if __name__ == "__main__":
    unittest.main(verbosity=2)
