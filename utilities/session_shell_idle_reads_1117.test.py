"""11-17: the panel backend does not re-read ledgers that have not changed.

The window's backend recomputes the panel on every shared-input change. It used to read every
session ledger again each time (the Steward mark and the request list), although the obligation
runner already knows how to reuse an unchanged ledger. This pins the three reuses the backend now
turns on, and that what the panel shows is unchanged by them:

* ``_backend()`` opts into unchanged-generation ledger reuse (``trust_unchanged_ledgers``);
* with that opt-in, ``steward.read_relations`` reuses its last result until a marker, a snapshot or
  a ledger changes, and a changed ledger alone is re-read; without the opt-in it reads every time;
* the request list reads predecessors from the published compact lineage rows, same result;
* the panel projection is the same with and without the reuse, for every harness.

Everything is synthetic in a private HOME; no real window, state or session is touched.
"""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import contextlib
import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "utilities"), str(ROOT / "tools")]
spec = importlib.util.spec_from_file_location("session_shell_idle_1117", ROOT / "utilities/session_shell.py")
shell = importlib.util.module_from_spec(spec)
sys.modules["session_shell_idle_1117"] = shell
spec.loader.exec_module(shell)

import dispatch_seat_handover as handover  # noqa: E402
import peer_request  # noqa: E402
import session_tidy  # noqa: E402
from fleet.collectors import steward  # noqa: E402
from peer_read_context import PeerReadContext  # noqa: E402

SID = "00000000-0000-4000-8000-0000000011e7"


class Isolated(unittest.TestCase):
    """A private HOME/XDG/dispatch root per test; the trust opt-in is restored afterwards."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="idle-reads-1117-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        saved = dict(os.environ)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(saved)))
        for key in tuple(os.environ):
            if key.startswith(("AGENT_", "HERDR_", "OPENCODE_", "CLAUDE_", "CODEX_", "XDG_", "HARNESS_")):
                os.environ.pop(key, None)
        self.home = self.root / "home"
        self.home.mkdir()
        os.environ["HOME"] = str(self.home)
        os.environ["XDG_STATE_HOME"] = str(self.home / ".local" / "state")
        os.environ["AGENT_PEER_LEDGER_ROOT"] = str(self.root / "peer")
        self.dispatch_root = self.root / "dispatch"
        self.dispatch_root.mkdir()
        self.jobs = self.dispatch_root / "jobs.log"
        self.jobs.touch()
        os.environ["AGENT_DISPATCH_JOBS"] = str(self.jobs)
        handover._LINEAGE_GRAPH_CACHE.clear()
        handover._TRANSITION_ROWS.clear()
        self.addCleanup(handover._LINEAGE_GRAPH_CACHE.clear)
        self.addCleanup(handover._TRANSITION_ROWS.clear)
        steward._RELATIONS_OBSERVATION = None
        self.addCleanup(setattr, steward, "_RELATIONS_OBSERVATION", None)
        self.sessions = session_tidy.state_root() / "sessions"
        self.sessions.mkdir(parents=True)

    def trust(self, on=True):
        token = handover._TRUST_LEDGER_GENERATIONS.set(on)
        self.addCleanup(handover._TRUST_LEDGER_GENERATIONS.reset, token)

    def write_ledger(self, key, rows):
        path = self.sessions / f"{key}.jsonl"
        path.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in rows))
        return path

    def append(self, key, row):
        with (self.sessions / f"{key}.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, sort_keys=True) + "\n")

    def fill(self, count=30):
        for index in range(count):
            self.write_ledger(f"seat-{index:03d}", [
                {"ts": 1, "harness": "claude", "sid": f"s-{index}", "event": "start", "source": "startup"},
                {"ts": 2, "harness": "claude", "sid": f"s-{index}", "event": "prompt"}])

    def lineage_fixture(self):
        """A bound-free chain a -> b -> c (two successions) plus unrelated noise ledgers."""
        self.fill(12)
        self.write_ledger("seat-chain", [
            {"ts": 10, "harness": "claude", "sid": "claude-a", "event": "start", "source": "startup"},
            {"ts": 11, "harness": "claude", "sid": "claude-b", "event": "succession",
             "source": "tidy-clear", "from": "claude-a"},
            {"ts": 12, "harness": "claude", "sid": "claude-c", "event": "succession",
             "source": "tidy-clear", "from": "claude-b"},
        ])


class BackendTrustsLedgers(Isolated):
    def test_backend_opts_into_ledger_reuse_before_serving_requests(self):
        seen = []

        def operation(request):
            seen.append(handover.trusting_unchanged_ledgers())
            return {"ok": True}
        # _backend sets the opt-in on this thread's context; run it in a copied context so
        # the test does not leak it into other tests.
        import contextvars
        lines = io.StringIO('{"id":1,"op":"x"}\n{"id":2,"op":"x"}\n')

        def run():
            self.assertFalse(handover.trusting_unchanged_ledgers())
            with mock.patch.object(shell, "_backend_operation", operation), \
                    mock.patch.object(sys, "stdin", lines), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(shell._backend(), 0)
            return handover.trusting_unchanged_ledgers()
        self.assertTrue(contextvars.copy_context().run(run))
        self.assertEqual(seen, [True, True])
        self.assertFalse(handover.trusting_unchanged_ledgers())

    def test_backend_still_serves_when_the_opt_in_is_unavailable(self):
        lines = io.StringIO('{"id":7,"op":"x"}\n')
        import contextvars

        def run():
            with mock.patch.object(handover, "trust_unchanged_ledgers", side_effect=RuntimeError("no")), \
                    mock.patch.object(shell, "_backend_operation", return_value={"ok": True}), \
                    mock.patch.object(sys, "stdin", lines), contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(shell._backend(), 0)
            return out.getvalue()
        self.assertEqual(json.loads(contextvars.copy_context().run(run)), {"id": 7, "ok": True})


class RelationsReuse(Isolated):
    def setUp(self):
        super().setUp()
        self.marker_dir = self.root / "peer-steward" / "codex"
        self.marker_dir.mkdir(parents=True)
        self.marker_path = self.marker_dir / "holder.json"
        self.marker = dict(session_id="holder", harness="codex", seat_key="fixture", generation="gen",
                           targets={"self": dict(source="explicit", kind="explicit", harness="unknown", name="-")})
        self.marker_path.write_text(json.dumps(self.marker))
        self.fill(20)
        self.write_ledger("fixture", [{"ts": 1, "harness": "codex", "sid": "holder", "event": "start"}])
        pm = SimpleNamespace(
            steward_marker_roots=lambda: ([self.root], "fixture"),
            read_steward_markers=lambda _roots: {("codex", "holder"): json.loads(self.marker_path.read_text())},
            steward_evidence_targets=lambda value: list(value["targets"].values()))
        for patcher in (mock.patch.object(steward, "_peer_message_module", return_value=pm),
                        mock.patch.object(handover, "_peer_message", return_value=pm)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def counted(self):
        return mock.patch.object(handover, "resolved_steward_relations",
                                 wraps=handover.resolved_steward_relations)

    def test_trusting_reader_reuses_until_a_marker_or_ledger_changes(self):
        self.trust()
        with self.counted() as read:
            first = steward.read_relations()
            self.assertEqual(len(first), 1)
            first[0]["holder"]["sid"] = "caller mutation"
            self.assertEqual(steward.read_relations()[0]["holder"]["sid"], "holder")
            self.assertEqual(read.call_count, 1)
            self.marker["generation"] = "new-generation"
            self.marker_path.write_text(json.dumps(self.marker))
            self.assertEqual(steward.read_relations()[0]["generation"], "new-generation")
            self.assertEqual(read.call_count, 2)
            self.append("fixture", dict(ts=5, event="steward-release", sid="holder", harness="codex",
                                        generation="new-generation", seat_key="fixture"))
            self.assertTrue(steward.read_relations()[0]["released"])
            self.assertEqual(read.call_count, 3)
            # An unrelated ledger's append is a change too: nothing is guessed to be irrelevant.
            self.append("seat-003", {"ts": 9, "harness": "claude", "sid": "s-3", "event": "prompt"})
            steward.read_relations()
            self.assertEqual(read.call_count, 4)
            steward.read_relations()
            self.assertEqual(read.call_count, 4)
            # A whole-file replacement is seen as well.
            replacement = self.sessions / "fixture.new"
            replacement.write_text(json.dumps(dict(ts=6, event="start", sid="holder", harness="codex")) + "\n")
            replacement.replace(self.sessions / "fixture.jsonl")
            self.assertFalse(steward.read_relations()[0]["released"])
            self.assertEqual(read.call_count, 5)

    def test_reader_that_did_not_opt_in_reads_every_time(self):
        self.assertFalse(handover.trusting_unchanged_ledgers())
        with self.counted() as read:
            steward.read_relations()
            steward.read_relations()
        self.assertEqual(read.call_count, 2)

    def test_unchanged_ledgers_are_not_parsed_again_and_one_append_reads_only_that_ledger(self):
        self.trust()
        # A marker that names no seat is the legacy form: the resolver then reads every ledger.
        self.marker.pop("seat_key")
        self.marker_path.write_text(json.dumps(self.marker))
        reads = []
        original = session_tidy._read_ledger_lines

        def read(seat, **options):
            reads.append(seat.key)
            return original(seat, **options)
        with mock.patch.object(session_tidy, "_read_ledger_lines", read):
            steward.role_flag("claude", "s-1")
            self.assertGreaterEqual(len(reads), 20)
            reads.clear()
            steward.role_flag("claude", "s-1")
            self.assertEqual(reads, [])
            self.append("seat-007", {"ts": 9, "harness": "claude", "sid": "s-7", "event": "prompt"})
            steward.role_flag("claude", "s-1")
            self.assertEqual(reads, ["seat-007"])


class CompactPredecessors(Isolated):
    def predecessors(self, harness, sid):
        return sorted(peer_request._predecessors(harness, sid))

    def test_predecessors_use_published_rows_and_match_the_full_read(self):
        self.lineage_fixture()
        handover.lineage_graph()  # warm: the comparison below is of results, not of cost
        full = {sid: self.predecessors("claude", sid) for sid in ("claude-a", "claude-b", "claude-c")}
        self.assertEqual(full["claude-c"], ["claude-a", "claude-b"])
        # Only the chain's end answers for the earlier sessions (verified forward).
        self.assertEqual(full["claude-b"], [])
        self.assertEqual(full["claude-a"], [])
        handover.publish_lineage_rows(self.dispatch_root)
        handover._LINEAGE_GRAPH_CACHE.clear()
        with mock.patch.object(handover, "_build_lineage_graph_uncached",
                               wraps=handover._build_lineage_graph_uncached) as build, \
                mock.patch.object(handover, "_readable_lineage_signature",
                                  wraps=handover._readable_lineage_signature) as full_read:
            compact = {sid: self.predecessors("claude", sid) for sid in ("claude-a", "claude-b", "claude-c")}
            # The published rows cover every ledger, so no ledger bytes are read for the full graph.
            full_read.assert_not_called()
            self.assertEqual(build.call_args.kwargs.get("compact_rows") is not None, True)
        self.assertEqual(compact, full)

    def test_without_published_rows_the_full_read_gives_the_same_list(self):
        self.lineage_fixture()
        self.assertEqual(self.predecessors("claude", "claude-c"), ["claude-a", "claude-b"])

    def test_a_ledger_changed_after_publishing_is_read_fresh(self):
        self.lineage_fixture()
        handover.publish_lineage_rows(self.dispatch_root)
        self.append("seat-chain", {"ts": 13, "harness": "claude", "sid": "claude-d", "event": "succession",
                                   "source": "tidy-clear", "from": "claude-c"})
        self.assertEqual(self.predecessors("claude", "claude-d"), ["claude-a", "claude-b", "claude-c"])
        self.assertEqual(self.predecessors("claude", "claude-c"), [])

    def test_follow_passes_the_context_only_when_given(self):
        self.lineage_fixture()
        handover.publish_lineage_rows(self.dispatch_root)
        endpoint = {"harness": "claude", "session_id": "claude-a"}
        self.assertEqual(peer_request.follow(endpoint)["session_id"], "claude-c")
        self.assertEqual(peer_request.follow(endpoint, read_context=PeerReadContext(compact_lineage=True))["session_id"],
                         "claude-c")
        with mock.patch.object(handover, "session_successor", return_value=("", "")) as successor:
            peer_request.follow(endpoint)
        self.assertIsNone(successor.call_args.kwargs["read_context"])


class SameScreen(Isolated):
    def project(self, harness):
        # The route glyph turns with the clock; hold it still so the two are comparable.
        with mock.patch("time.time", return_value=1_800_000_000.0), \
                mock.patch.object(session_tidy, "prompt_display", return_value={"received": []}):
            return shell.panel_projection(harness, SID, columns=100, rows=40)

    def test_projection_is_the_same_with_and_without_ledger_reuse_for_every_harness(self):
        self.lineage_fixture()
        self.write_ledger("seat-own", [{"ts": 1, "harness": "claude", "sid": SID, "event": "start"}])
        handover.publish_lineage_rows(self.dispatch_root)
        for harness in ("claude", "codex", "opencode"):
            with self.subTest(harness=harness):
                handover._TRANSITION_ROWS.clear()
                handover._LINEAGE_GRAPH_CACHE.clear()
                steward._RELATIONS_OBSERVATION = None
                fresh = self.project(harness)
                token = handover._TRUST_LEDGER_GENERATIONS.set(True)
                try:
                    first = self.project(harness)
                    second = self.project(harness)
                finally:
                    handover._TRUST_LEDGER_GENERATIONS.reset(token)
                self.assertEqual(json.dumps(first, sort_keys=True, default=str),
                                 json.dumps(fresh, sort_keys=True, default=str))
                self.assertEqual(json.dumps(second, sort_keys=True, default=str),
                                 json.dumps(fresh, sort_keys=True, default=str))
                self.assertIn("panel", fresh)


if __name__ == "__main__":
    unittest.main()
