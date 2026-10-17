#!/usr/bin/env python3
"""Unit tests for utilities/peer-message.py (SD-122 peer-message ledger)."""
import hashlib
import contextlib
import errno
import fcntl
import io
import importlib.util
import json
import os
import shlex
import sqlite3
import stat
import sys
import tempfile
import time
import unittest
import threading
from unittest import mock
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_SPEC = importlib.util.spec_from_file_location("peer_message", str(_HERE / "peer-message.py"))
peer_message = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(peer_message)


class _TmpRootMixin:
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_root = Path(self._tmp.name)
        self.jobs_path = self.tmp_root / "jobs.log"
        self.jobs_path.touch()
        self._old_environ = dict(os.environ)
        os.environ["AGENT_DISPATCH_JOBS"] = str(self.jobs_path)
        os.environ["AGENT_PEER_LEDGER_ROOT"] = str(self.tmp_root / "peer-state")
        # C-9 makes `_iter_records` include dispatch and installed-runtime roots.
        # Isolate each root explicitly so list/status assertions cannot read live
        # peer records from this machine.
        os.environ["HOME"] = str(self.tmp_root / "home")
        os.environ["XDG_STATE_HOME"] = str(self.tmp_root / "xdg-state")
        os.environ["XDG_CONFIG_HOME"] = str(self.tmp_root / "xdg-config")
        os.environ["HARNESS_STATE_ROOT"] = str(self.tmp_root / "harness-state")
        os.environ["CODEX_HOME"] = str(self.tmp_root / "home" / ".codex")
        os.environ["CLAUDE_CONFIG_DIR"] = str(self.tmp_root / "home" / ".claude")
        os.environ.pop("AGENT_HOME", None)
        self.addCleanup(self._restore_environ)

    def _restore_environ(self):
        os.environ.clear()
        os.environ.update(self._old_environ)

    def _record(self, body="hello world\nsecond line", **overrides):
        args = dict(
            from_harness="claude", from_session_id="sid-a", from_project="proj",
            to_harness="claude", to_session_id="sid-b", to_name=None,
            kind="steer", surface="claude-native", status="sent", receipt=None,
            ref=[], body_file=None, body_stdin=True,
        )
        args.update(overrides)
        ns = peer_message.argparse.Namespace(**args)
        old_stdin = sys.stdin
        sys.stdin = _FakeStdin(body)
        try:
            return peer_message.cmd_record(ns)
        finally:
            sys.stdin = old_stdin

    def _ledger_file(self, from_sid="sid-a"):
        return peer_message._ledger_path(from_sid)


class _FakeStdin:
    def __init__(self, data):
        self._data = data

    def isatty(self):
        return False

    def read(self):
        return self._data


class PeerMessageTest(_TmpRootMixin, unittest.TestCase):
    def test_record_writes_exactly_one_line(self):
        rc = self._record()
        self.assertEqual(rc, 0)
        lines = self._ledger_file().read_text().splitlines()
        self.assertEqual(len(lines), 1)

    def test_append_only(self):
        self._record()
        first = self._ledger_file().read_text().splitlines()[0]
        self._record()
        lines = self._ledger_file().read_text().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[0], first)

    def test_schema_v1_fields(self):
        self._record()
        rec = json.loads(self._ledger_file().read_text().splitlines()[0])
        for key in ("schema_version", "message_id", "ts", "from", "to", "kind",
                    "summary", "body_sha256", "delivery", "refs"):
            self.assertIn(key, rec)
        self.assertEqual(rec["schema_version"], 1)
        self.assertEqual(len(rec["message_id"]), 16)
        int(rec["message_id"], 16)

    def test_same_second_repeated_records_get_distinct_message_ids(self):
        # Same sender/target/second-resolution ts/summary — the classic
        # steward-wait collision (its reproduction log left the repo with evidence/).
        to = {"session_id": "sid-b"}
        ts = "2026-09-02T00:00:00Z"
        id_a = peer_message._message_id("sid-a", to, ts, "same summary")
        id_b = peer_message._message_id("sid-a", to, ts, "same summary")
        self.assertNotEqual(id_a, id_b)
        for mid in (id_a, id_b):
            self.assertEqual(len(mid), 16)
            int(mid, 16)

    def test_summary_hard_truncated_at_200(self):
        body = "x" * 500
        self._record(body=body)
        rec = json.loads(self._ledger_file().read_text().splitlines()[0])
        self.assertEqual(len(rec["summary"]), 200)

    def test_body_text_absent_body_sha256_present(self):
        body = "first line only\nsuper-secret-body-text-marker\nline3"
        self._record(body=body)
        raw = self._ledger_file().read_text()
        self.assertNotIn("super-secret-body-text-marker", raw)
        rec = json.loads(raw.splitlines()[0])
        self.assertEqual(rec["body_sha256"], hashlib.sha256(body.encode()).hexdigest())

    def test_flock_serializes_concurrent_writers(self):
        import multiprocessing

        def worker(n):
            for i in range(20):
                self._record(body=f"worker {n} msg {i}")

        procs = [multiprocessing.Process(target=worker, args=(n,)) for n in range(2)]
        for p in procs:
            p.start()
        for p in procs:
            p.join()
        lines = self._ledger_file().read_text().splitlines()
        self.assertEqual(len(lines), 40)
        for line in lines:
            json.loads(line)

    def test_list_skips_malformed_line(self):
        self._record(body="one")
        self._record(body="two")
        path = self._ledger_file()
        with open(path, "a") as fh:
            fh.write("{not json\n")
        ns = peer_message.argparse.Namespace(since_hours=None, limit=None)
        recs = list(peer_message._iter_records())
        self.assertEqual(len(recs), 2)

    def test_status_skips_malformed_line(self):
        self._record(body="one")
        path = self._ledger_file()
        with open(path, "a") as fh:
            fh.write("{also not json\n")
        ns = peer_message.argparse.Namespace(since_hours=None)
        rc = peer_message.cmd_status(ns)
        self.assertEqual(rc, 0)

    def test_unwritable_state_root_is_fail_soft(self):
        root = peer_message.peer_state_root() / "peer-messages"
        root.mkdir(parents=True, exist_ok=True)
        os.chmod(root, stat.S_IREAD | stat.S_IEXEC)
        try:
            rc = self._record()
            self.assertNotEqual(rc, 0)
        finally:
            os.chmod(root, stat.S_IRWXU)

    def test_state_root_comes_from_resolver(self):
        self._record()
        path = self._ledger_file()
        self.assertTrue(str(path).startswith(str(self.tmp_root)))

    def test_path_layout(self):
        self._record()
        path = self._ledger_file()
        self.assertEqual(path.name, "sid-a.jsonl")
        self.assertRegex(path.parent.name, r"^\d{4}-\d{2}$")
        self.assertEqual(path.parent.parent.name, "peer-messages")


class PeerMessageEnumValidationTest(_TmpRootMixin, unittest.TestCase):
    def test_invalid_kind_rejected_no_record_typed_stderr(self):
        import io
        import contextlib

        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            rc = self._record(kind="not-a-kind")
        self.assertNotEqual(rc, 0)
        self.assertIn("peer-message: invalid-kind", stderr.getvalue())
        self.assertFalse(self._ledger_file().exists())

    def test_invalid_surface_rejected_no_record_typed_stderr(self):
        import io
        import contextlib

        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            rc = self._record(surface="unknown")
        self.assertNotEqual(rc, 0)
        self.assertIn("peer-message: invalid-surface", stderr.getvalue())
        self.assertFalse(self._ledger_file().exists())

    def test_invalid_status_rejected_no_record_typed_stderr(self):
        import io
        import contextlib

        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            rc = self._record(status="not-a-status")
        self.assertNotEqual(rc, 0)
        self.assertIn("peer-message: invalid-status", stderr.getvalue())
        self.assertFalse(self._ledger_file().exists())

    def test_valid_enum_values_all_pass(self):
        for kind in peer_message._KINDS:
            for surface in peer_message._SURFACES:
                for status in peer_message._STATUSES:
                    rc = self._record(kind=kind, surface=surface, status=status,
                                       from_session_id=f"sid-{kind}-{surface}-{status}")
                    self.assertEqual(rc, 0)

    def test_hook_call_site_values_are_in_enum(self):
        self.assertIn("claude-native", peer_message._SURFACES)
        self.assertIn("herdr", peer_message._SURFACES)
        self.assertIn("received", peer_message._STATUSES)
        self.assertIn("sent", peer_message._STATUSES)

    def test_surface_required_by_argparse(self):
        parser_argv = [
            "record",
            "--from-harness", "claude", "--from-session-id", "sid-a",
            "--to-harness", "claude", "--to-name", "peer-1",
        ]
        with self.assertRaises(SystemExit):
            peer_message.main(parser_argv)


class PeerMessageBodyFlagTest(_TmpRootMixin, unittest.TestCase):
    def test_no_body_flag_does_not_touch_stdin_and_returns_empty(self):
        class _RaisingStdin:
            def isatty(self):
                return False

            def read(self):
                raise AssertionError("stdin must not be read when no body flag is given")

        args = dict(
            from_harness="claude", from_session_id="sid-a", from_project="proj",
            to_harness="claude", to_session_id="sid-b", to_name=None,
            kind="steer", surface="claude-native", status="sent", receipt=None,
            ref=[], body_file=None, body_stdin=False,
        )
        ns = peer_message.argparse.Namespace(**args)
        old_stdin = sys.stdin
        sys.stdin = _RaisingStdin()
        try:
            rc = peer_message.cmd_record(ns)
        finally:
            sys.stdin = old_stdin
        self.assertEqual(rc, 0)
        rec = json.loads(self._ledger_file().read_text().splitlines()[0])
        self.assertEqual(rec["summary"], "")
        self.assertEqual(rec["body_sha256"], hashlib.sha256(b"").hexdigest())

    def test_body_stdin_flag_reads_stdin(self):
        args = dict(
            from_harness="claude", from_session_id="sid-a", from_project="proj",
            to_harness="claude", to_session_id="sid-b", to_name=None,
            kind="steer", surface="claude-native", status="sent", receipt=None,
            ref=[], body_file=None, body_stdin=True,
        )
        ns = peer_message.argparse.Namespace(**args)
        old_stdin = sys.stdin
        sys.stdin = _FakeStdin("piped body\nsecond")
        try:
            rc = peer_message.cmd_record(ns)
        finally:
            sys.stdin = old_stdin
        self.assertEqual(rc, 0)
        rec = json.loads(self._ledger_file().read_text().splitlines()[0])
        self.assertEqual(rec["summary"], "piped body")


def _rec(sid, to_sid, summary, ts="2026-09-01T00:00:00Z"):
    return {
        "schema_version": 1, "message_id": summary, "ts": ts,
        "from": {"harness": "claude", "session_id": sid, "project": "p"},
        "to": {"harness": "claude", "session_id": to_sid}, "kind": "steer",
        "summary": summary, "body_sha256": "x",
        "delivery": {"surface": "herdr", "status": "sent", "receipt": None}, "refs": [],
    }


class IterRecordsMultiRootTest(_TmpRootMixin, unittest.TestCase):
    """C-9 — `_iter_records` (the engine behind `list`/`status`) reads the SAME
    root set every other peer-ledger consumer reads (`steward_marker_roots`), not
    just the one root new writes land under. C-1 moves only where NEW records
    land; it never migrates history, so a record still sitting under the old
    dispatch-registry root must not silently disappear from `list`/`status`."""

    def _write_shard(self, root, sid, rec):
        month_dir = Path(root) / "peer-messages" / time.strftime("%Y-%m", time.gmtime())
        month_dir.mkdir(parents=True, exist_ok=True)
        with open(month_dir / ("%s.jsonl" % sid), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec) + "\n")

    def test_iter_records_reads_both_the_new_and_the_old_root(self):
        old_root = peer_message._ledger_root()      # AGENT_DISPATCH_JOBS-derived (pre-C-1 root)
        new_root = peer_message.peer_state_root()   # AGENT_PEER_LEDGER_ROOT (C-1 canonical root)
        self.assertNotEqual(str(old_root), str(new_root))
        self._write_shard(old_root, "sid-old", _rec("sid-old", "sid-x", "old-root"))
        self._write_shard(new_root, "sid-new", _rec("sid-new", "sid-y", "new-root"))

        recs = list(peer_message._iter_records())
        self.assertEqual({r["summary"] for r in recs}, {"old-root", "new-root"})

        # A root that appears twice in the resolver's list must not double its records.
        with mock.patch.object(peer_message, "steward_marker_roots",
                                return_value=([Path(new_root), Path(new_root)], "test")):
            recs_dup = list(peer_message._iter_records())
        self.assertEqual(len(recs_dup), 1)
        self.assertEqual(recs_dup[0]["summary"], "new-root")

        # cmd_list/cmd_status go through the same engine and see both records too.
        recs_by_cmd = list(peer_message._iter_records(since_hours=None))
        self.assertEqual(len(recs_by_cmd), 2)


class ReadTransferRecordCrossRootTest(_TmpRootMixin, unittest.TestCase):
    """C-4 — sender and receiver processes commonly run under different
    per-runtime `AGENT_DISPATCH_JOBS` (§2.2: a Codex managed session inherits its
    own dispatch tree). Before C-1, `_transfer_path` followed that same
    per-process value, so a sender and receiver with different
    `AGENT_DISPATCH_JOBS` wrote/read two different transfer-record roots. C-1
    anchors `_transfer_path` to `peer_state_root()` instead, which both
    processes resolve to the SAME value regardless of `AGENT_DISPATCH_JOBS` —
    this is what actually lets the receiver's `parse_peer_trailer` recover the
    sender's identity."""

    def test_transfer_record_written_under_one_dispatch_root_is_read_under_another(self):
        recipient = {"harness": "codex", "session_id": "recipient-a"}
        sender = {"harness": "codex", "session_id": "sender-a", "name": "s"}
        text, ref = peer_message.prepare_peer_message("body", sender, recipient)
        own_path = peer_message._transfer_path(ref)
        self.assertTrue(own_path.exists())
        # Simulate a receiving process with a different AGENT_DISPATCH_JOBS (its own
        # runtime's dispatch root) while AGENT_PEER_LEDGER_ROOT — the value that
        # actually governs `_transfer_path` since C-1 — stays the same.
        other_jobs = self.tmp_root / "other-runtime" / "jobs.log"
        other_jobs.parent.mkdir(parents=True)
        other_jobs.touch()
        os.environ["AGENT_DISPATCH_JOBS"] = str(other_jobs)
        self.assertEqual(peer_message._transfer_path(ref), own_path)
        parsed = peer_message.parse_peer_trailer(text, recipient)
        self.assertEqual(parsed["session_id"], sender["session_id"])


class F100cSenderAndStewardTest(unittest.TestCase):
    """F-100c — from.name, the herdr sender trailer, and the steward marker."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self._old = dict(os.environ)
        os.environ["AGENT_DISPATCH_JOBS"] = str(self.root / "jobs.log")
        os.environ["AGENT_PEER_LEDGER_ROOT"] = str(self.root)
        # See `_TmpRootMixin.setUp` — isolates every read candidate
        # `steward_marker_roots()`'s fleet-reader branch can add.
        os.environ["HOME"] = str(self.root / "home")
        os.environ.pop("XDG_STATE_HOME", None)
        os.environ.pop("HARNESS_STATE_ROOT", None)
        os.environ.pop("CODEX_HOME", None)
        os.environ.pop("CLAUDE_CONFIG_DIR", None)
        (self.root / "jobs.log").touch()
        os.environ.pop("AGENT_HOME", None)
        self.addCleanup(self._restore)

    def _restore(self):
        os.environ.clear()
        os.environ.update(self._old)

    def _ns(self, **over):
        base = dict(from_harness="claude", from_session_id="sid-a", from_project="p",
                    from_name=None, to_harness="codex", to_session_id="sid-c", to_name="child",
                    kind="steer", surface="herdr", status="sent", receipt=None, ref=[],
                    body_file=None, body_stdin=False)
        base.update(over)
        return peer_message.argparse.Namespace(**base)

    def _records(self):
        out = []
        for f in (peer_message.peer_state_root() / "peer-messages").rglob("*.jsonl"):
            out += [json.loads(l) for l in f.read_text().splitlines() if l.strip()]
        return out

    def test_from_name_is_additive(self):
        self.assertEqual(peer_message.cmd_record(self._ns()), 0)
        self.assertEqual(peer_message.cmd_record(self._ns(from_name="hearting-46")), 0)
        recs = self._records()
        self.assertNotIn("name", recs[0]["from"])
        self.assertEqual(recs[1]["from"]["name"], "hearting-46")

    def test_trailer_round_trip_and_last_wins(self):
        t = peer_message.peer_trailer("claude", "sid-a", "hearting-46")
        self.assertIn("(peer-from: claude [?] hearting-46)", t)
        self.assertEqual(peer_message.parse_peer_trailer("body\n\n" + t),
                         {"harness": "claude", "session_id": None, "name": "hearting-46"})
        self.assertEqual(peer_message.parse_peer_trailer("(peer-from: codex 01a0)")["name"], None)
        two = "(peer-from: codex one)\nx\n(peer-from: claude two n)"
        self.assertEqual(peer_message.parse_peer_trailer(two)["session_id"], "two")
        self.assertIsNone(peer_message.parse_peer_trailer("no trailer here"))
        self.assertIsNone(peer_message.parse_peer_trailer(None))
        # a name with a ')' cannot break the envelope
        self.assertEqual(peer_message.parse_peer_trailer(
            peer_message.peer_trailer("claude", "s", "a) b"))["name"], "a  b".replace("  ", " "))

    def _marker_keys_on_disk(self):
        return {(h, d.get("session_id")) for h, _p, d in
                peer_message._iter_marker_files(peer_message.peer_state_root()) if d}

    def test_record_never_marks_the_sender_whatever_the_kind(self):
        """Role ≠ communication (user 2026-09-06: every session that talked showed d=−1).
        A worker's `[handoff]` to its steward is a message; so is a SendMessage with
        `notify_when_idle`, which the Claude hook records as `kind=watch` (review #1)."""
        for kind in peer_message._KINDS:
            for status in ("sent", "delivered", "received"):
                self.assertEqual(peer_message.cmd_record(self._ns(kind=kind, status=status)), 0)
        self.assertEqual(peer_message.read_steward_markers(), {})
        self.assertEqual(self._marker_keys_on_disk(), set())
        self.assertFalse(peer_message.steward_marker_path("claude", "sid-a").exists())
        self.assertFalse((peer_message.peer_state_root() / "peer-steward").exists())

    def test_mark_steward_requires_an_evidence_source_and_release_clears(self):
        ts = "2026-09-06T00:00:00Z"
        to = {"harness": "codex", "session_id": "sid-c", "name": "child"}
        self.assertFalse(peer_message.mark_steward("claude", "sid-a", to, "handoff", ts))
        self.assertFalse(peer_message.mark_steward("claude", "sid-a", to, "watch", ts))       # no source
        self.assertFalse(peer_message.mark_steward("claude", "sid-a", to, "steer", ts, source="steer"))
        self.assertFalse(peer_message.mark_steward("claude", "", to, "watch", ts, source="watch"))
        self.assertFalse(peer_message.steward_marker_path("claude", "sid-a").exists())
        self.assertTrue(peer_message.mark_steward("claude", "sid-a", to, "start", ts, source="start"))
        self.assertTrue(peer_message.mark_steward("claude", "sid-a", to, "watch", ts, source="watch"))
        self.assertTrue(peer_message.mark_steward("claude", "sid-a",
                                                  {"harness": "unknown", "name": "-"},
                                                  "explicit", ts, source="explicit"))
        marker = peer_message.read_steward_markers()[("claude", "sid-a")]
        self.assertEqual({e["source"] for e in marker["targets"].values()}, {"watch", "explicit"})
        self.assertEqual(marker["targets"]["sid-c"]["harness"], "codex")
        self.assertEqual(marker["schema_version"], 2)
        self.assertEqual(peer_message.cmd_release(peer_message.argparse.Namespace(
            harness="claude", session_id="sid-a")), 0)
        self.assertEqual(peer_message.read_steward_markers(), {})

    def test_schema_version_2_only_when_every_entry_carries_a_source(self):
        path = self._write_marker("claude", "sid-a", {
            "h": {"harness": "claude", "session_id": "h", "name": "h", "kind": "handoff", "ts": "1"}})
        to = {"harness": "codex", "session_id": "w", "name": "w"}
        self.assertTrue(peer_message.mark_steward("claude", "sid-a", to, "watch", "2", source="watch"))
        data = json.loads(path.read_text())
        self.assertEqual(data["schema_version"], 1)      # a source-less entry remains
        self.assertEqual({e.get("source") for e in data["targets"].values()}, {None, "watch"})
        self.assertTrue(peer_message.mark_steward("claude", "sid-b", to, "watch", "2", source="watch"))
        self.assertEqual(json.loads(peer_message.steward_marker_path("claude", "sid-b").read_text())
                         ["schema_version"], 2)

    def _write_marker(self, harness, sid, targets, updated="2026-09-06T00:00:00Z"):
        path = peer_message.steward_marker_path(harness, sid)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"schema_version": 1, "harness": harness, "session_id": sid,
                                    "since": updated, "updated": updated, "targets": targets}))
        return path

    def test_legacy_markers_are_judged_by_kind_and_handoff_only_ones_are_ignored(self):
        """Markers the old rule left behind carry no `source`: a wait/watch entry (kind=watch)
        and the old `steward on` placeholder (kind=watch, name '-') still count; entries from
        steer/handoff/gate-relay sends do not — no migration, the reader just stops believing
        them."""
        self._write_marker("claude", "worker-1", {
            "w1:p15": {"harness": "claude", "session_id": "steward-1", "name": "hearting-b0",
                       "kind": "handoff", "ts": "2026-09-06T01:00:00Z"},
            "x": {"harness": "codex", "session_id": "x", "name": "x", "kind": "steer",
                  "ts": "2026-09-06T02:00:00Z"}})
        self._write_marker("claude", "steward-1", {
            "child-a": {"harness": "codex", "session_id": "child-a", "name": "a", "kind": "handoff",
                        "ts": "2026-09-06T03:00:00Z"},
            "child-b": {"harness": "claude", "session_id": "child-b", "name": "b", "kind": "watch",
                        "ts": "2026-09-06T01:00:00Z"},
            "-": {"harness": "unknown", "session_id": None, "name": "-", "kind": "watch",
                  "ts": "2026-09-06T02:00:00Z"}})
        self._write_marker("opencode", "oc-1", {})
        self._write_marker("claude", "old-launcher", {
            "c": {"harness": "codex", "session_id": "c", "name": "c", "kind": "steer",
                  "ts": "2026-09-06T01:00:00Z"}})          # the old `start` wrote a steer row
        self.assertEqual(set(peer_message.read_steward_markers()), {("claude", "steward-1")})
        self.assertEqual(self._marker_keys_on_disk(),
                         {("claude", "worker-1"), ("claude", "steward-1"), ("opencode", "oc-1"),
                          ("claude", "old-launcher")})
        self.assertFalse(peer_message.is_steward_marker({"session_id": "z", "targets": {
            "q": {"kind": "start", "ts": "1"}}}))                # legacy kind=start is not evidence
        marker = peer_message.read_steward_markers()[("claude", "steward-1")]
        self.assertEqual([e["session_id"] for e in peer_message.steward_evidence_targets(marker)],
                         ["child-b", None])
        self.assertFalse(peer_message.is_steward_marker({"session_id": "z", "targets": {
            "q": {"kind": "watch", "source": "handoff"}}}))       # a source that is not evidence
        self.assertTrue(peer_message.is_steward_marker({"session_id": "z", "targets": {
            "q": {"kind": "handoff", "source": "explicit"}}}))    # source wins over kind
        self.assertEqual(peer_message.steward_evidence_targets(None), [])
        self.assertEqual(peer_message.steward_evidence_targets({"targets": "junk"}), [])

    def test_prune_steward_markers_dry_run_lists_and_apply_removes_only_unevidenced(self):
        """Every run here names the temp root explicitly (`--root`): the default root set is
        the Fleet reader's, which includes the real per-user root."""
        root = str(peer_message.peer_state_root())
        stale = self._write_marker("claude", "worker-1", {
            "s": {"harness": "claude", "session_id": "s", "name": "n", "kind": "handoff", "ts": "1"}})
        empty = self._write_marker("claude", "worker-2", {})
        keep = self._write_marker("claude", "steward-1", {
            "c": {"harness": "codex", "session_id": "c", "name": "c", "kind": "watch", "ts": "1"}})
        broken = keep.parent / "broken.json"
        broken.write_text("{not json")
        with mock.patch("builtins.print") as print_mock:
            self.assertEqual(peer_message.main(["prune-steward-markers", "--root", root]), 2)
        lines = [c[0][0] for c in print_mock.call_args_list]
        self.assertEqual(lines[0], f"root={root}")
        self.assertTrue(lines[-1].startswith("prune-steward-markers mode=dry-run roots=1 "
                                             "roots_source=explicit markers=4 unevidenced=2 "
                                             "removed=0 unreadable=1"), lines[-1])
        cands = [l for l in lines if l.startswith("candidate ")]
        self.assertEqual(sorted(l.split()[2] for l in cands), ["worker-1", "worker-2"])
        self.assertIn("kinds=handoff", next(l for l in cands if " worker-1 " in l))
        for p in (stale, empty, keep, broken):
            self.assertTrue(p.exists())
        # a flag raised between the scan and the unlink survives (review #7)
        real_unlink = peer_message._unlink_unevidenced_marker
        def racing_unlink(path):
            if path == empty:
                peer_message.mark_steward("claude", "worker-2", {"harness": "codex", "session_id": "z",
                                                                 "name": "z"}, "watch", "9", source="watch")
            return real_unlink(path)
        with mock.patch("builtins.print") as print_mock, \
             mock.patch.object(peer_message, "_unlink_unevidenced_marker", side_effect=racing_unlink):
            self.assertEqual(peer_message.main(["prune-steward-markers", "--apply", "--root", root]), 0)
        lines = [c[0][0] for c in print_mock.call_args_list]
        self.assertTrue(lines[-1].startswith("prune-steward-markers mode=apply roots=1 "
                                             "roots_source=explicit markers=4 unevidenced=2 "
                                             "removed=1 unreadable=1"), lines[-1])
        self.assertTrue(any(l.startswith("removed claude worker-1 ") for l in lines))
        self.assertTrue(any(l.startswith("kept:now-evidenced claude worker-2 ") for l in lines))
        self.assertFalse(stale.exists())
        self.assertTrue(empty.exists()); self.assertTrue(keep.exists()); self.assertTrue(broken.exists())
        # clean root → dry-run exits 0
        with mock.patch("builtins.print") as print_mock:
            self.assertEqual(peer_message.main(["prune-steward-markers", "--root", root]), 0)
        self.assertIn("unevidenced=0", print_mock.call_args[0][0])

    def test_prune_default_roots_are_the_fleet_readers_and_are_printed(self):
        roots, source = peer_message.steward_marker_roots()
        self.assertEqual(source, "fleet-reader")
        self.assertEqual(roots[0], peer_message.peer_state_root())     # chain index 0 = the writer's root
        fake = self.root / "fake-root"
        (fake / "peer-steward" / "codex").mkdir(parents=True)
        (fake / "peer-steward" / "codex" / "t.json").write_text(json.dumps(
            {"session_id": "t", "targets": {"a": {"kind": "steer", "ts": "1"}}}))
        with mock.patch("builtins.print") as print_mock, \
             mock.patch.object(peer_message, "steward_marker_roots", return_value=([fake], "fleet-reader")):
            self.assertEqual(peer_message.main(["prune-steward-markers"]), 2)
        lines = [c[0][0] for c in print_mock.call_args_list]
        self.assertEqual(lines[0], f"root={fake}")
        self.assertIn("roots=1 roots_source=fleet-reader markers=1 unevidenced=1", lines[-1])
        self.assertTrue((fake / "peer-steward" / "codex" / "t.json").exists())

    def test_claude_session_name_reads_the_registry_by_session_id(self):
        cfg = self.root / "cfg"
        (cfg / "sessions").mkdir(parents=True)
        (cfg / "sessions" / "123.json").write_text(json.dumps(
            {"sessionId": "sid-a", "name": "hearting-46", "nameSource": "derived"}))
        (cfg / "sessions" / "bad.json").write_text("{not json")
        self.assertEqual(peer_message.claude_session_name("sid-a", config_dir=str(cfg)), "hearting-46")
        self.assertIsNone(peer_message.claude_session_name("sid-zz", config_dir=str(cfg)))
        self.assertIsNone(peer_message.claude_session_name("", config_dir=str(cfg)))


class UsableSessionIdTest(_TmpRootMixin, unittest.TestCase):
    """F-101i input guard — a value that cannot identify a session never becomes one.

    Measured 2026-09-03: a sender emitted the trailer's documentation form verbatim, so
    `(peer-from: claude <sid> hearting-46)` put the literal string `<sid>` in four ledger
    records and created a `<sid>.jsonl` shard beside the real per-session ones.
    """

    def test_real_ids_pass_through_unchanged(self):
        for sid in ("841d1f64-0cff-4c16-8578-e874a45a2bb6", "01a064d8-e0e6-7ac2-97df-877c380ce013",
                    "ses_7f3a19c2b0004e11", "sid-a"):
            with self.subTest(sid=sid):
                self.assertEqual(peer_message.usable_session_id(sid), sid)

    def test_placeholders_and_absent_markers_are_not_identities(self):
        for sid in ("<sid>", "-", "", "   ", None, "<session_id>", ".", ".."):
            with self.subTest(sid=sid):
                self.assertIsNone(peer_message.usable_session_id(sid))

    def test_a_separator_never_reaches_the_ledger_filename(self):
        for sid in ("../../escape", "a/b", "a\\b", "a b"):
            with self.subTest(sid=sid):
                self.assertIsNone(peer_message.usable_session_id(sid))

    def test_trailer_parse_drops_an_unsubstituted_placeholder(self):
        parsed = peer_message.parse_peer_trailer("body\n(peer-from: claude <sid> hearting-46)")
        self.assertEqual(parsed["harness"], "claude")
        self.assertIsNone(parsed["session_id"])
        self.assertEqual(parsed["name"], "hearting-46")

    def test_trailer_parse_keeps_a_real_id(self):
        parsed = peer_message.parse_peer_trailer(
            "body\n(peer-from: claude 841d1f64-0cff-4c16-8578-e874a45a2bb6 hearting-18)")
        self.assertEqual(parsed["session_id"], "841d1f64-0cff-4c16-8578-e874a45a2bb6")

    def test_the_module_own_absent_marker_round_trips_as_absent(self):
        """`peer_trailer` writes `-` when it has no id; parsing it back must not mint one."""
        text = peer_message.peer_trailer("claude", None, "hearting-46")
        self.assertIn("(peer-from: claude [?]", text)
        self.assertIsNone(peer_message.parse_peer_trailer(text)["session_id"])

    def test_unusable_sender_shards_to_unknown_not_to_a_junk_sibling(self):
        for sid in ("<sid>", "-", ""):
            with self.subTest(sid=sid):
                self.assertEqual(peer_message._ledger_path(sid).name, "unknown.jsonl")
        self.assertEqual(peer_message._ledger_path("sid-a").name, "sid-a.jsonl")

    def test_record_stores_no_placeholder_on_either_endpoint(self):
        rc = peer_message.cmd_record(peer_message.argparse.Namespace(
            from_harness="claude", from_session_id="<sid>", from_project="hearting",
            from_name="hearting-46", to_harness="claude", to_session_id="<sid>",
            to_name="hearting-18", kind="notice", surface="herdr", status="received",
            receipt=None, ref=[], body_file=None, body_stdin=False))
        self.assertEqual(rc, 0)
        shard = peer_message._ledger_path("")
        rec = json.loads(shard.read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(rec["from"]["session_id"], "")
        self.assertNotIn("session_id", rec["to"])
        # The name is still carried, so `✉` counters and the notice keep working.
        self.assertEqual(rec["from"]["name"], "hearting-46")


class PendingPeerTest(_TmpRootMixin, unittest.TestCase):
    sender = {"harness": "codex", "session_id": "sender", "name": "original"}
    recipient = {"harness": "codex", "session_id": "recipient"}

    def prepare_reference(self, body, recipient=None, sender=None, **kwargs):
        sender = sender or self.sender
        recipient = recipient or self.recipient
        evidence = {
            "schema": "peer-read-access-observation-v1",
            "observed_at": time.monotonic(),
            "recipient": {"harness": recipient["harness"],
                          "session_id": recipient["session_id"], "pane_id": "w1:pTest"},
            "local_process": {"pid": os.getpid(), "start": "1", "argv": ["codex"],
                              "pid_namespace": "test"},
        }
        with mock.patch.object(peer_message, "_reference_local_process_matches", return_value=True):
            return peer_message.prepare_peer_message(
                body, sender, recipient, reference_evidence=evidence, **kwargs)

    def pending(self, body="original peer body", **kwargs):
        return self.prepare_reference(body, defer=True, **kwargs)

    def install_native_observation(self, ref, text, *, item_json=None):
        home = Path(os.environ["CODEX_HOME"])
        control = home / "app-server-control"
        sessions = home / "sessions"
        control.mkdir(parents=True)
        sessions.mkdir()
        home.chmod(0o700)
        control.chmod(0o700)
        sessions.chmod(0o700)
        rollout = sessions / "fixture.jsonl"
        rollout.write_bytes(b"")
        rollout.chmod(0o600)

        state_path = home / "state_5.sqlite"
        state = sqlite3.connect(state_path)
        state.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT, history_mode TEXT)")
        state.execute("INSERT INTO threads VALUES (?, ?, 'paginated')",
                      (self.recipient["session_id"], str(rollout)))
        state.commit()
        state.close()
        state_path.chmod(0o600)

        item = {"type": "userMessage", "clientId": "delivery-peer-" + ref,
                "content": [{"type": "text", "text": text, "text_elements": []}]}
        serialized_item = item_json if item_json is not None else json.dumps(item)
        history_path = home / "thread_history_1.sqlite"
        history = sqlite3.connect(history_path)
        history.executescript("""
            CREATE TABLE thread_turns (
                thread_id TEXT NOT NULL, turn_id TEXT NOT NULL, status TEXT,
                PRIMARY KEY(thread_id, turn_id)
            );
            CREATE TABLE thread_items (
                thread_id TEXT NOT NULL, turn_id TEXT NOT NULL, item_id TEXT NOT NULL,
                item_json TEXT NOT NULL, item_type TEXT NOT NULL, rollout_ordinal INTEGER,
                PRIMARY KEY(thread_id, turn_id, item_id)
            );
            CREATE INDEX idx_thread_items_user_messages
                ON thread_items(thread_id, rollout_ordinal) WHERE item_type='userMessage';
            CREATE TABLE thread_history_projection_state (
                thread_id TEXT PRIMARY KEY, next_rollout_byte_offset INTEGER,
                next_rollout_ordinal INTEGER
            );
        """)
        thread_id = self.recipient["session_id"]
        history.execute("INSERT INTO thread_turns VALUES (?, 'turn-1', 'completed')", (thread_id,))
        history.execute("INSERT INTO thread_items VALUES (?, 'turn-1', 'item-1', ?, 'userMessage', 1)",
                        (thread_id, serialized_item))
        history.execute("INSERT INTO thread_history_projection_state VALUES (?, 0, 2)",
                        (thread_id,))
        history.commit()
        history.close()
        history_path.chmod(0o600)

    def test_recipient_scan_only_checks_settlement_for_exact_receiver_or_sender(self):
        _text, wanted = self.pending()
        _text, foreign = self.pending("foreign", recipient={
            "harness": "claude", "session_id": "another-recipient"}, sender={
                "harness": "claude", "session_id": "foreign-sender"})
        seen = []

        def settled(row, **_kwargs):
            seen.append(row["ref"])
            return False

        with mock.patch.object(peer_message, "_registered_delivery_settled", side_effect=settled):
            rows = list(peer_message._pending_rows(recipient=self.recipient))
        self.assertEqual([row["ref"] for row in rows], [wanted])
        self.assertEqual(seen, [wanted])

        seen.clear()
        with mock.patch.object(peer_message, "_registered_delivery_settled", side_effect=settled):
            rows = list(peer_message._pending_rows(sender=self.sender))
        self.assertEqual([row["ref"] for row in rows], [wanted])
        self.assertEqual(seen, [wanted])
        self.assertNotEqual(wanted, foreign)

    def test_filtered_scan_still_quarantines_malformed_foreign_rows(self):
        _text, wanted = self.pending()
        outside = self.tmp_root / "outside"
        outside.write_bytes(b"foreign")
        malformed = peer_message._pending_dir() / ("f" * 32 + ".json")
        malformed.symlink_to(outside)
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors):
            rows = list(peer_message._pending_rows(recipient=self.recipient))
        self.assertEqual([row["ref"] for row in rows], [wanted])
        self.assertIn("peer-pending-invalid", errors.getvalue())
        quarantine = malformed.parent.parent / "quarantine"
        self.assertEqual(len(list(quarantine.rglob(malformed.name))), 1)

    def test_production_read_scope_source_is_codex_adapter_contract_only(self):
        self.assertTrue(peer_message._codex_model_read_access(self.recipient))
        self.assertFalse(peer_message._codex_model_read_access(
            {"harness": "claude", "session_id": "claude-session"}))
        self.assertFalse(peer_message._codex_model_read_access(
            {"harness": "opencode", "session_id": "opencode-session"}))
        self.assertFalse(peer_message._codex_model_read_access(
            {"harness": "codex", "session_id": None}))

    def test_unknown_or_unproven_access_keeps_complete_source_inline(self):
        body = "[steer] complete request\n" + "detail " * 2000 + "\n실행하지 말 것; STFT 크기 하한 1e-5 유지"
        recipients = (
            {"harness": "unknown", "session_id": None},
            {"harness": "claude", "session_id": "other-runtime"},
            {"harness": "codex", "session_id": "sidless", "pane_id": "w1:pNew"},
            self.recipient,
        )
        for recipient in recipients:
            with self.subTest(recipient=recipient):
                text, ref = peer_message.prepare_peer_message(
                    body, self.sender, recipient, defer=True)
                self.assertIn(body.rstrip("\n"), text)
                self.assertLessEqual(len(text.encode("utf-8")), peer_message._PENDING_BYTES)
                self.assertIsNone(peer_message._read_transfer_record(ref).get("body_ref"))


    def test_private_payload_preserves_body_endpoints_refs_and_digest(self):
        text, ref = self.pending(refs=["original-proof"])
        row = peer_message._read_pending(ref)
        self.assertEqual(row["text"], text)
        self.assertEqual(row["from"], self.sender)
        self.assertEqual(row["to"], self.recipient)
        self.assertEqual(row["refs"], ["original-proof"])
        self.assertEqual(row["body_sha256"], hashlib.sha256(text.encode()).hexdigest())
        self.assertEqual(stat.S_IMODE(peer_message._pending_path(ref).stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(peer_message._pending_dir().stat().st_mode), 0o700)
        self.assertFalse(self._ledger_file("sender").exists())

    def test_long_message_reference_preserves_raw_bytes_and_survives_receipt(self):
        body = ("[steer] header\r\n" + "중요한 설명 with English data " * 140
                + "\r\n실행하지 말 것\r\nSTFT 크기 하한 1e-5·위상 유지\n")
        text, ref = self.pending(body, refs=["proof-ref"])
        row = peer_message._read_pending(ref)
        transfer = peer_message._read_transfer_record(ref)
        payload = peer_message.read_peer_payload(ref)
        path = Path(payload["path"])
        raw = body.encode("utf-8")

        self.assertLessEqual(len(text.encode("utf-8")), peer_message._BODY_REFERENCE_ENVELOPE_BYTES)
        self.assertNotIn("STFT 크기 하한", text)
        self.assertIn(str(path), text)
        self.assertEqual(payload["body"], body)
        self.assertEqual(path.read_bytes(), raw)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
        self.assertEqual(row["body_ref"], transfer["body_ref"])
        self.assertEqual(row["body_sha256"], hashlib.sha256(text.encode("utf-8")).hexdigest())
        self.assertEqual(row["source_sha256"], hashlib.sha256(body.rstrip("\n").encode("utf-8")).hexdigest())
        self.assertEqual(transfer["body_ref"]["raw_size_bytes"], len(raw))
        self.assertEqual(transfer["body_ref"]["payload_sha256"], hashlib.sha256(raw).hexdigest())
        self.assertEqual(transfer["body_ref"]["source_sha256"], row["source_sha256"])
        self.assertNotIn(body, json.dumps(transfer, ensure_ascii=False))

        self.assertEqual(peer_message.receive_peer_message(text, self.recipient), 0)
        received = peer_message._read_pending(ref)
        self.assertEqual(received["state"], "received")
        self.assertIsNone(received["text"])
        self.assertEqual(path.read_bytes(), raw)
        self.assertEqual(peer_message.read_peer_payload(ref)["body"], body)

    def test_reference_threshold_is_utf8_bytes_and_no_trailer_stays_inline(self):
        bodies = ("한" * 682 + "a", "한" * 682 + "ab", "한" * 683)
        self.assertEqual([len(body.encode("utf-8")) for body in bodies], [2047, 2048, 2049])
        for body in bodies:
            with self.subTest(raw_bytes=len(body.encode("utf-8"))):
                text, ref = self.pending(body)
                descriptor = peer_message._read_transfer_record(ref).get("body_ref")
                self.assertEqual(descriptor is not None, len(body.encode("utf-8")) > 2048)
                if descriptor:
                    self.assertLessEqual(len(text.encode("utf-8")), 1024)
                    self.assertEqual(peer_message.read_peer_payload(ref)["body"], body)
                else:
                    self.assertTrue(text.startswith(body))

        long_body = "inline only " * 300
        text, ref = peer_message.prepare_peer_message(
            long_body, self.sender, self.recipient, defer=True, with_trailer=False)
        self.assertEqual(text, long_body)
        self.assertNotIn("body_ref", peer_message._read_pending(ref))
        self.assertNotIn("body_ref", peer_message._read_transfer_record(ref))
        self.assertFalse((peer_message.peer_state_root() / "peer-messages" / "payloads" / (ref + ".txt")).exists())

    def test_long_retry_reuses_exact_reference_and_different_tail_does_not(self):
        body = "[steer] same preview\n" + "proof data " * 300 + "\nfinal condition A"
        text, ref = self.pending(body)
        retry = peer_message.prepare_peer_message(
            body, dict(self.sender, name="renamed"), self.recipient, defer=True)
        self.assertEqual(retry, (text, ref))
        changed = body[:-1] + "B"
        different = self.prepare_reference(changed, defer=True)
        self.assertNotEqual(different[1], ref)
        self.assertEqual(peer_message.read_peer_payload(different[1])["body"], changed)

    def test_payload_publication_failure_falls_back_to_complete_inline_body(self):
        body = "[steer] fallback\n" + "fallback detail " * 180
        with mock.patch.object(peer_message, "_write_body_payload", side_effect=PermissionError("read-only")):
            text, ref = self.pending(body)
        self.assertTrue(text.startswith(body.rstrip("\n")))
        self.assertNotIn("body_ref", peer_message._read_pending(ref))
        self.assertNotIn("body_ref", peer_message._read_transfer_record(ref))
        self.assertEqual(peer_message.receive_peer_message(text, self.recipient), 0)

    def test_missing_or_changed_reference_payload_never_receipts(self):
        body = "[steer] message\n" + "integrity proof " * 200
        text, ref = self.pending(body)
        path = Path(peer_message.read_peer_payload(ref)["path"])
        original = path.read_bytes()
        path.write_bytes(b"X" + original[1:])
        with self.assertRaisesRegex(ValueError, "digest-invalid"):
            peer_message._read_pending(ref)
        self.assertEqual(peer_message.receive_peer_message(text, self.recipient), 1)
        pending = json.loads(peer_message._pending_path(ref).read_text())
        self.assertEqual(pending["state"], "pending")
        self.assertFalse(self._ledger_file("sender").exists())

        path.write_bytes(original)
        path.chmod(0o644)
        with self.assertRaisesRegex(ValueError, "file-unsafe"):
            peer_message._read_pending(ref)
        path.chmod(0o600)
        path.unlink()
        path.symlink_to(self.tmp_root / "outside")
        with self.assertRaises(OSError):
            peer_message._read_pending(ref)

    def test_ambiguous_payload_roots_fail_closed(self):
        body = "[steer] duplicate roots\n" + "same payload " * 200
        _text, ref = self.pending(body)
        source = Path(peer_message.read_peer_payload(ref)["path"])
        duplicate_root = self.tmp_root / "other-state"
        duplicate = duplicate_root / "peer-messages" / "payloads" / (ref + ".txt")
        duplicate.parent.mkdir(mode=0o700, parents=True)
        duplicate.write_bytes(source.read_bytes())
        duplicate.chmod(0o600)
        with mock.patch.object(peer_message, "steward_marker_roots", return_value=([duplicate_root], "fixture")):
            with self.assertRaisesRegex(ValueError, "ambiguous"):
                peer_message.read_peer_payload(ref)

    def test_pending_retry_reuses_ref_but_different_body_or_sid_does_not(self):
        text, ref = self.pending()
        renamed = dict(self.sender, name="renamed")
        self.assertEqual(peer_message.prepare_peer_message("original peer body", renamed, self.recipient), (text, ref))
        self.assertNotEqual(self.pending("different body")[1], ref)
        for changed in (dict(self.recipient, session_id="fork"), dict(self.recipient, harness="claude")):
            self.assertNotEqual(peer_message.prepare_peer_message("original peer body", self.sender, changed, defer=True)[1], ref)
            self.assertNotIn(ref, [r["ref"] for r in peer_message.pending_messages(changed)])

    def test_sidless_pending_stays_on_its_exact_pane_until_a_session_is_observed(self):
        recipient = {"harness": "codex", "session_id": None, "name": "fresh",
                     "pane_id": "w1:pX"}
        body = "[steer] unresolved pane\n" + "detail " * 400 + "\n실행하지 말 것; STFT 크기 하한 1e-5"
        text, ref = peer_message.prepare_peer_message(
            body, self.sender, recipient, defer=True)
        row = peer_message._read_pending(ref)
        self.assertEqual(row["state"], "pending")
        self.assertEqual(row["to"], recipient)
        self.assertIn(body.rstrip("\n"), text)
        self.assertIsNone(peer_message._read_transfer_record(ref).get("body_ref"))
        self.assertIsNone(peer_message.pending_delivery_recipient(row))
        self.assertIsNone(peer_message.bind_pending_recipient(
            ref, {"harness": "codex", "session_id": "new-thread", "pane_id": "w1:pOther"}))
        bound = peer_message.bind_pending_recipient(
            ref, {"harness": "codex", "session_id": "new-thread", "pane_id": "w1:pX"})
        self.assertEqual(peer_message.pending_delivery_recipient(bound), bound["bound_to"])
        self.assertEqual(bound["text"], text)
        self.assertIsNone(bound.get("body_ref"))
        parsed = peer_message.parse_peer_trailer(
            text, {"harness": "codex", "session_id": "new-thread"}, include_ref=True)
        self.assertEqual(parsed["transfer_ref"], ref)
        self.assertIsNone(peer_message.bind_pending_recipient(
            ref, {"harness": "codex", "session_id": "different-thread", "pane_id": "w1:pX"}))

    def test_no_trailer_pending_is_observed_as_queued_without_claiming_receiver_ack(self):
        recipient = {"harness": "claude", "session_id": None, "name": "fresh",
                     "pane_id": "w2:p1"}
        text, ref = peer_message.prepare_peer_message(
            "plain user message", self.sender, recipient, defer=True, with_trailer=False)
        self.assertEqual(text, "plain user message")
        row = peer_message._read_pending(ref)
        self.assertTrue(row["no_trailer"])
        bound = peer_message.bind_pending_recipient(
            ref, {"harness": "claude", "session_id": "resolved-session", "pane_id": "w2:p1"})
        claim = peer_message.claim_pending_herdr(ref, peer_message.pending_delivery_recipient(bound))
        self.assertIsNotNone(claim)
        self.assertTrue(peer_message.mark_pending_queued(ref))
        current = peer_message._read_pending(ref)
        self.assertEqual(current["state"], "queued")
        self.assertIsNone(current.get("rpc_claim"))
        self.assertIsNone(peer_message.parse_peer_trailer(text, {
            "harness": "claude", "session_id": "resolved-session"}, include_ref=True))

    def test_exact_claude_prompt_claim_can_be_queued_but_only_receiver_ref_acks(self):
        recipient = {"harness": "claude", "session_id": "claude-target"}
        text, ref = peer_message.prepare_peer_message(
            "one guarded native prompt", self.sender, recipient, defer=True)
        claimed = peer_message.claim_pending_herdr(ref, recipient)
        self.assertIsNotNone(claimed)
        foreign_claim = dict(claimed["rpc_claim"], token="different-claim")
        self.assertFalse(peer_message.mark_pending_prompt_queued(ref, foreign_claim))
        self.assertEqual(peer_message._read_pending(ref)["state"], "unverified")

        self.assertTrue(peer_message.mark_pending_prompt_queued(ref, claimed["rpc_claim"]))
        queued = peer_message._read_pending(ref)
        self.assertEqual((queued["state"], queued["receipt"], queued["text"]),
                         ("queued", "claude-pty-prompt-submitted", text))
        self.assertIsNone(queued.get("rpc_claim"))
        self.assertEqual(peer_message.receive_peer_message(text, recipient), 0)
        received = peer_message._read_pending(ref)
        self.assertEqual((received["state"], received["receipt"]),
                         ("received", "exact-peer-ref"))

    def test_claude_prompt_queue_transition_rejects_no_trailer_and_other_receipts(self):
        recipient = {"harness": "claude", "session_id": "claude-target"}
        _text, ref = peer_message.prepare_peer_message(
            "no trailer", self.sender, recipient, defer=True, with_trailer=False)
        claimed = peer_message.claim_pending_herdr(ref, recipient)
        self.assertIsNotNone(claimed)
        self.assertFalse(peer_message.mark_pending_prompt_queued(ref, claimed["rpc_claim"]))
        self.assertFalse(peer_message.mark_pending_prompt_queued(
            ref, claimed["rpc_claim"], receipt="invented-receive"))
        self.assertEqual(peer_message._read_pending(ref)["state"], "unverified")

    def test_opencode_receive_requires_the_queued_native_id_and_is_idempotent(self):
        recipient = {"harness": "opencode", "session_id": "opencode-root"}
        _text, ref = peer_message.prepare_peer_message(
            "이어서해", self.sender, recipient, defer=True, with_trailer=False,
            receipt="session-tidy-continue")
        claim = peer_message.argparse.Namespace(
            to_harness="opencode", to_session_id="opencode-root", claim=ref,
            queued=None, received=None, message_id=None)
        with mock.patch("builtins.print"):
            self.assertEqual(peer_message.cmd_pending(claim), 0)
        queued = peer_message.argparse.Namespace(
            to_harness="opencode", to_session_id="opencode-root", claim=None,
            queued=ref, received=None, message_id="native-message-1")
        self.assertEqual(peer_message.cmd_pending(queued), 0)
        resuming = peer_message.argparse.Namespace(
            to_harness="opencode", to_session_id="opencode-root", claim=None,
            queued=None, resuming=ref, received=None, message_id="native-message-1")
        self.assertEqual(peer_message.cmd_pending(resuming), 0)
        self.assertEqual(peer_message.cmd_pending(resuming), 1)
        self.assertTrue(peer_message._read_pending(ref)["resume_attempted"])
        received = peer_message.argparse.Namespace(
            to_harness="opencode", to_session_id="opencode-root", claim=None, resuming=None,
            queued=None, received=ref, message_id="foreign-message")
        self.assertEqual(peer_message.cmd_pending(received), 1)
        self.assertEqual(peer_message._read_pending(ref)["state"], "queued")
        received.message_id = "native-message-1"
        self.assertEqual(peer_message.cmd_pending(received), 0)
        original = self._ledger_file(self.sender["session_id"]).read_bytes()
        row = peer_message._read_pending(ref)
        self.assertEqual((row["state"], row["text"], row["actual_message_id"]),
                         ("received", None, "native-message-1"))
        self.assertEqual(peer_message.cmd_pending(received), 0)
        self.assertEqual(self._ledger_file(self.sender["session_id"]).read_bytes(), original)

    def test_actual_receive_once_and_replay_does_not_rewrite_notice(self):
        text, ref = self.pending()
        self.assertEqual(peer_message.receive_peer_message(text, self.recipient), 0)
        original = self._ledger_file("sender").read_bytes()
        received = peer_message._read_pending(ref)
        with mock.patch.object(peer_message, "_ack_registered_delivery",
                               wraps=peer_message._ack_registered_delivery) as repair_ack:
            self.assertEqual(peer_message.receive_peer_message(text, self.recipient), 0)
            repair_ack.assert_called_once_with(received)
        self.assertEqual(self._ledger_file("sender").read_bytes(), original)
        self.assertEqual(peer_message._read_pending(ref)["state"], "received")
        self.assertIsNone(peer_message._read_pending(ref)["text"])
        row = json.loads(original)
        self.assertEqual(row["transfer_ref"], ref)
        self.assertEqual(row["delivery"]["status"], "received")
        self.assertNotIn("original peer body", original.decode())
        self.assertNotEqual(self.pending()[1], ref) # A later intentional message is new.

    def test_herdr_claim_is_atomic_and_receiver_ack_clears_it(self):
        recipient = dict(self.recipient, harness="claude")
        text, ref = peer_message.prepare_peer_message("deferred", self.sender, recipient, defer=True)
        ready = threading.Barrier(2)
        winners = []
        def claim():
            ready.wait(timeout=3)
            try:
                winners.append(peer_message.claim_pending_herdr(ref, recipient))
            except BlockingIOError:
                winners.append(None)
        workers = [threading.Thread(target=claim) for _ in range(2)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=3)
            self.assertFalse(worker.is_alive())
        self.assertEqual(sum(row is not None for row in winners), 1)
        row = peer_message._read_pending(ref)
        self.assertEqual(row["state"], "unverified")
        self.assertEqual(row["text"], text)
        self.assertIsNotNone(row["rpc_claim"])
        self.assertIsNone(peer_message.claim_pending_herdr(ref, recipient))
        self.assertEqual(peer_message.receive_peer_message(text, recipient), 0)
        received = peer_message._read_pending(ref)
        self.assertEqual(received["state"], "received")
        self.assertIsNone(received["rpc_claim"])
        self.assertEqual(received["receipt"], "exact-peer-ref")
        self.assertIsNone(peer_message.claim_pending_herdr(ref, recipient))

    def test_herdr_claim_rejects_a_different_recipient(self):
        _text, ref = self.pending()
        self.assertIsNone(peer_message.claim_pending_herdr(ref, dict(self.recipient, session_id="other")))
        self.assertEqual(peer_message._read_pending(ref)["state"], "pending")

    def test_unsent_claim_can_retry_but_receiver_ack_is_never_undone(self):
        text, ref = self.pending()
        first = peer_message.claim_pending_herdr(ref, self.recipient)
        self.assertTrue(peer_message.release_unsent_herdr_claim(first))
        second = peer_message.claim_pending_herdr(ref, self.recipient)
        self.assertIsNotNone(second)
        self.assertFalse(peer_message.release_unsent_herdr_claim(first))
        self.assertEqual(peer_message.receive_peer_message(text, self.recipient), 0)
        self.assertFalse(peer_message.release_unsent_herdr_claim(second))
        self.assertEqual(peer_message._read_pending(ref)["state"], "received")

    def test_claude_paste_wrapper_keeps_exact_ref_and_body(self):
        recipient = dict(self.recipient, harness="claude")
        for index, closing in enumerate(('</pasted_content>', '</pasted_content id="fe20">')):
            with self.subTest(closing=closing):
                text, ref = peer_message.prepare_peer_message(
                    "긴 원문 " * 60 + str(index), self.sender, recipient, defer=True)
                wrapped = '<pasted_content id="fe20">\n' + text + '\n' + closing
                peer_message.receive_peer_message(wrapped.replace("긴 원문", "변경", 1), recipient)
                self.assertEqual(peer_message._read_pending(ref)["state"], "pending")
                peer_message.receive_peer_message(wrapped, dict(recipient, session_id="other"))
                self.assertEqual(peer_message._read_pending(ref)["state"], "pending")
                peer_message.receive_peer_message("앞에 다른 본문\n" + wrapped, recipient)
                self.assertEqual(peer_message._read_pending(ref)["state"], "pending")
                self.assertEqual(peer_message.receive_peer_message(wrapped, recipient), 0)
                self.assertEqual(peer_message._read_pending(ref)["state"], "received")

    def test_foreign_sid_or_changed_body_never_ack_pending(self):
        text, ref = self.pending()
        for actual, body in ((dict(self.recipient, session_id="fork"), text),
                             (self.recipient, "changed " + text)):
            peer_message.receive_peer_message(body, actual)
            self.assertEqual(peer_message._read_pending(ref)["state"], "pending")

    def test_payload_tamper_permissions_symlink_and_oversize_refused(self):
        text, ref = self.pending()
        path = peer_message._pending_path(ref)
        original = path.read_bytes()
        row = json.loads(original)
        row["to"]["session_id"] = "other"
        path.write_text(json.dumps(row))
        with self.assertRaisesRegex(ValueError, "binding-invalid"):
            peer_message._read_pending(ref)
        path.write_bytes(original)
        path.chmod(0o644)
        with self.assertRaisesRegex(ValueError, "file-unsafe"):
            peer_message._read_pending(ref)
        path.chmod(0o600)
        path.unlink()
        path.symlink_to(self.tmp_root / "outside")
        with self.assertRaises(OSError):
            peer_message._read_pending(ref)
        path.unlink() # Remove this fixture's unsafe link before independent size check.
        long_text, long_ref = self.pending("x" * 16384)
        self.assertLessEqual(len(long_text.encode("utf-8")), 1024)
        self.assertEqual(len(peer_message.read_peer_payload(long_ref)["body"]), 16384)

    def test_same_ref_claim_is_nonblocking_without_blocking_other_recipient(self):
        import codex_queue_delivery as queue
        text, ref = self.pending()
        other = dict(self.recipient, session_id="other-recipient")
        with peer_message.pending_lock(ref):
            with self.assertRaises(BlockingIOError):
                peer_message.deliver_pending_codex(ref)
            ordinary = peer_message.prepare_peer_message("ordinary body", self.sender, other)
            deferred = peer_message.prepare_peer_message("deferred body", self.sender, other, defer=True)
            with mock.patch.object(queue, "send_at_least_once", return_value={"status": "queued"}):
                self.assertEqual(peer_message.deliver_pending_codex(deferred[1])["status"], "queued")
        self.assertTrue(ordinary[0].startswith("ordinary body"))
        self.assertEqual(peer_message._read_pending(ref)["text"], text)

    def test_unrelated_damaged_row_does_not_block_healthy_prepare_or_drain(self):
        text, ref = self.pending()
        damaged = peer_message._pending_dir() / ("0" * 32 + ".json")
        damaged.write_text("malformed original")
        damaged.chmod(0o600)
        recipient = dict(self.recipient, session_id="other")
        with mock.patch("builtins.print") as diagnostic:
            ordinary = peer_message.prepare_peer_message("healthy ordinary", self.sender, recipient)
            deferred = peer_message.prepare_peer_message("healthy deferred", self.sender, recipient, defer=True)
        self.assertEqual(sum(call.args == ("peer-pending-invalid ref=" + "0" * 32,)
                             for call in diagnostic.call_args_list), 1)
        self.assertEqual(peer_message.deliver_pending_codex("0" * 32)["status"], "unsupported")
        self.assertTrue(ordinary[0].startswith("healthy ordinary"))
        self.assertEqual([r["ref"] for r in peer_message.pending_messages(recipient)], [deferred[1]])
        self.assertEqual(peer_message.receive_peer_message(text, self.recipient), 0)
        preserved = list((damaged.parent.parent / "quarantine").rglob(damaged.name))
        self.assertEqual(len(preserved), 1)
        self.assertEqual(preserved[0].read_text(), "malformed original")
        self.assertFalse(damaged.exists())
        self.assertEqual(peer_message._read_pending(ref)["state"], "received")

    def legacy_shell_pending(self, recipient, sender=None):
        sender = sender if sender is not None else {"harness": "unknown", "session_id": "", "name": None}
        text, ref = peer_message.prepare_peer_message("external notification", sender, recipient, defer=True)
        path = peer_message._pending_path(ref)
        row = json.loads(path.read_text())
        # Exact format left by the old writer; no mutation of the transfer seal.
        row.update(state="unverified", receipt="peer-endpoint-unverified")
        peer_message._save_pending(row)
        return text, ref

    def test_legacy_shell_sender_is_delivered_and_received_once_on_each_harness(self):
        for harness in ("claude", "codex", "opencode"):
            with self.subTest(harness=harness):
                recipient = dict(self.recipient, harness=harness)
                text, ref = self.legacy_shell_pending(recipient)
                path = peer_message._pending_path(ref)
                original = path.read_bytes()
                errors = io.StringIO()
                with contextlib.redirect_stderr(errors):
                    for _ in range(2):
                        self.assertEqual([r["ref"] for r in peer_message.pending_messages(recipient)], [ref])
                self.assertEqual(errors.getvalue(), "")
                self.assertEqual(path.read_bytes(), original)  # Read compatibility is not a repair write.
                self.assertEqual(peer_message._read_pending(ref)["state"], "pending")
                parsed = peer_message.parse_peer_trailer(text, recipient, include_ref=True)
                self.assertIsNone(parsed["session_id"])
                self.assertEqual(parsed["transfer_ref"], ref)
                peer_message.receive_peer_message(text, dict(recipient, session_id="foreign"))
                peer_message.receive_peer_message("changed " + text, recipient)
                self.assertEqual(path.read_bytes(), original)
                peer_message.receive_peer_message(text, recipient)
                notice = self._ledger_file("").read_bytes()
                peer_message.receive_peer_message(text, recipient)
                self.assertEqual(self._ledger_file("").read_bytes(), notice)
                row = peer_message._read_pending(ref)
                self.assertEqual(row["state"], "received")
                self.assertIsNone(row["text"])
                self.assertEqual(row["from"], {"harness": "unknown", "session_id": "", "name": None})

    def test_new_shell_sender_uses_normal_pending_for_null_and_empty_forms(self):
        for sender in ({"harness": "unknown", "session_id": ""},
                       {"harness": None, "session_id": None}):
            for harness in ("claude", "codex", "opencode"):
                with self.subTest(sender=sender, harness=harness):
                    recipient = dict(self.recipient, harness=harness)
                    text, ref = peer_message.prepare_peer_message("shell notification", sender, recipient, defer=True)
                    self.assertEqual(peer_message._read_pending(ref)["state"], "pending")
                    self.assertEqual(peer_message.parse_peer_trailer(text, recipient, include_ref=True)["transfer_ref"], ref)

    def test_legacy_shell_sender_uses_existing_claude_context_and_codex_queue(self):
        recipient = dict(self.recipient, harness="claude")
        text, ref = self.legacy_shell_pending(recipient)
        stream = io.StringIO()
        peer_message.emit_peer_context(recipient, "UserPromptSubmit", stream=stream)
        context = json.loads(stream.getvalue())["hookSpecificOutput"]["additionalContext"]
        self.assertEqual(context, text)
        self.assertEqual(peer_message._read_pending(ref)["state"], "queued")
        peer_message.receive_peer_message(context, recipient)
        self.assertEqual(peer_message._read_pending(ref)["state"], "received")
        import codex_queue_delivery as queue
        text, ref = self.legacy_shell_pending(self.recipient)
        with mock.patch.object(queue, "send_at_least_once", return_value={"status": "queued"}) as send:
            self.assertEqual(peer_message.deliver_pending_codex(ref)["status"], "queued")
            self.assertTrue(send.call_args.kwargs["allow_add"])
        with mock.patch.object(queue, "send_at_least_once", return_value={"status": "consumed"}) as send:
            self.assertEqual(peer_message.deliver_pending_codex(ref)["status"], "received")
            self.assertFalse(send.call_args.kwargs["allow_add"])
        self.assertIsNone(peer_message._read_pending(ref)["text"])

    def test_unknown_sender_does_not_reset_ambiguous_send_or_invent_recipient(self):
        _text, ref = self.legacy_shell_pending(self.recipient)
        row = json.loads(peer_message._pending_path(ref).read_text())
        for receipt in ("herdr-delivery-inflight", "native-queue-inflight", "context-api-inflight"):
            peer_message._save_pending(dict(row, receipt=receipt))
            self.assertEqual(peer_message._read_pending(ref)["state"], "unverified")
            self.assertIsNone(peer_message.claim_pending_herdr(ref, self.recipient))
        peer_message._save_pending(dict(row, actual_message_id="accepted"))
        self.assertEqual(peer_message._read_pending(ref)["state"], "unverified")
        peer_message._save_pending(dict(row, rpc_claim={"token": "inflight"}))
        self.assertEqual(peer_message._read_pending(ref)["state"], "unverified")
        for recipient in ({"harness": "claude", "session_id": None},
                          {"harness": None, "session_id": None}):
            _text, missing = self.legacy_shell_pending(recipient)
            self.assertEqual(peer_message._read_pending(missing)["state"], "unverified")
            self.assertEqual(peer_message.pending_messages(recipient), [])

    def test_unknown_sender_compatibility_still_checks_binding_and_body(self):
        _text, ref = self.legacy_shell_pending(self.recipient)
        original = peer_message._pending_path(ref).read_bytes()
        for changed in ({"from": {"harness": "unknown", "session_id": "forged"}},
                        {"to": dict(self.recipient, session_id="foreign")}, {"text": "tampered"}):
            row = json.loads(original)
            row.update(changed)
            peer_message._save_pending(row)
            with self.assertRaisesRegex(ValueError, "peer-pending-(binding|body)-invalid"):
                peer_message._read_pending(ref)

    def test_quarantine_preserves_symlink_without_touching_its_target(self):
        outside = self.tmp_root / "outside"
        outside.write_bytes(b"unrelated bytes")
        damaged = peer_message._pending_dir() / ("0" * 32 + ".json")
        damaged.symlink_to(outside)
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors):
            for _ in range(2):
                self.assertEqual(list(peer_message._pending_rows()), [])
        self.assertEqual(errors.getvalue().count("peer-pending-invalid"), 1)
        preserved = list((damaged.parent.parent / "quarantine").rglob(damaged.name))
        self.assertEqual(len(preserved), 1)
        self.assertTrue(preserved[0].is_symlink())
        self.assertEqual(preserved[0].readlink(), outside)
        self.assertEqual(outside.read_bytes(), b"unrelated bytes")

    def test_quarantine_leaves_a_valid_successor_in_place(self):
        _text, ref = self.pending()
        path = peer_message._pending_path(ref)
        original = path.read_bytes()
        path.write_text("malformed")
        old = path.lstat()
        replacement = path.with_name(".replacement")
        replacement.write_bytes(original)
        replacement.chmod(0o600)
        os.replace(replacement, path)
        self.assertFalse(peer_message._quarantine_pending(path, old))
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual([r["ref"] for r in peer_message.pending_messages(self.recipient)], [ref])

    def test_locked_transfer_is_readable_on_the_next_callback(self):
        _text, ref = self.legacy_shell_pending(self.recipient)
        path = peer_message._pending_path(ref)
        original = path.read_bytes()
        errors = io.StringIO()
        with peer_message._transfer_path(ref).open() as seal, contextlib.redirect_stderr(errors):
            fcntl.flock(seal.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(peer_message.pending_messages(self.recipient), [])
            self.assertTrue(path.exists())
            self.assertEqual(path.read_bytes(), original)
        with contextlib.redirect_stderr(errors):
            self.assertEqual([r["ref"] for r in peer_message.pending_messages(self.recipient)], [ref])
        self.assertEqual(errors.getvalue(), "")
        self.assertEqual(path.read_bytes(), original)

    def test_temporary_read_failure_does_not_quarantine_a_healthy_row(self):
        _text, ref = self.pending()
        path = peer_message._pending_path(ref)
        original = path.read_bytes()
        errors = io.StringIO()
        with mock.patch.object(peer_message, "_read_transfer_record", side_effect=OSError(errno.EIO, "temporary read")), \
                contextlib.redirect_stderr(errors):
            self.assertEqual(peer_message.pending_messages(self.recipient), [])
        self.assertTrue(path.exists())
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual([r["ref"] for r in peer_message.pending_messages(self.recipient)], [ref])
        self.assertEqual(errors.getvalue(), "")

    def test_scan_skips_received_rows_without_verifying_them(self):
        # Delivered history piles up; verifying every row on each prompt cost seconds.
        text, received_ref = self.pending("delivered body")
        self.assertEqual(peer_message.receive_peer_message(text, self.recipient), 0)
        _text, open_ref = self.pending("still open body")
        real = peer_message._read_transfer_record
        with mock.patch.object(peer_message, "_read_transfer_record", side_effect=real) as reads:
            self.assertEqual([r["ref"] for r in peer_message.pending_messages(self.recipient)], [open_ref])
        self.assertEqual([c.args[0] for c in reads.call_args_list], [open_ref])
        self.assertEqual(peer_message._read_pending(received_ref)["state"], "received")

    def test_every_quarantined_row_is_diagnosed_once_in_a_batch(self):
        _text, healthy_ref = self.pending()
        originals = {}
        for index in range(4):
            ref = f"{index:032x}"
            path = peer_message._pending_dir() / (ref + ".json")
            raw = f"malformed original {index}".encode()
            path.write_bytes(raw)
            path.chmod(0o600)
            originals[ref] = raw
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors):
            for _ in range(2):
                self.assertEqual([r["ref"] for r in peer_message.pending_messages(self.recipient)], [healthy_ref])
        quarantine = peer_message._pending_dir().parent / "quarantine"
        self.assertEqual(len(list(quarantine.rglob("*.json"))), 4)
        for ref, raw in originals.items():
            self.assertEqual(errors.getvalue().count(f"peer-pending-invalid ref={ref}\n"), 1)
            self.assertEqual(next(quarantine.rglob(ref + ".json")).read_bytes(), raw)

    def test_acceptance_stays_queued_and_disables_restart_and_ambiguous_retry(self):
        import codex_queue_delivery as queue
        text, ref = self.pending()
        with mock.patch.object(queue, "send_at_least_once", return_value={"status": "queued"}) as send:
            result = peer_message.deliver_pending_codex(ref)
        self.assertEqual(result["status"], "queued")
        self.assertEqual(peer_message._read_pending(ref)["text"], text)
        self.assertFalse(self._ledger_file("sender").exists())
        self.assertEqual(send.call_args.kwargs["client_message_id"], "delivery-peer-" + ref)
        for field in ("allow_restart", "retry_ambiguous"):
            self.assertFalse(send.call_args.kwargs[field])

    def test_ambiguous_add_preserves_payload_and_next_attempt_is_observation_only(self):
        import codex_queue_delivery as queue
        text, ref = self.pending()
        with mock.patch.object(queue, "send_at_least_once", side_effect=queue.QueueDeliveryError("lost-response", ambiguous=True)):
            self.assertEqual(peer_message.deliver_pending_codex(ref)["status"], "unverified")
        self.assertEqual(peer_message._read_pending(ref)["text"], text)
        with mock.patch.object(queue, "send_at_least_once", return_value={"status": "queued"}) as send:
            peer_message.deliver_pending_codex(ref)
        self.assertFalse(send.call_args.kwargs["allow_add"])

    def test_queue_runtime_access_loss_keeps_envelope_and_requests_context_fallback(self):
        import codex_queue_delivery as queue
        body = "[steer] queued original\n" + "long detail " * 260 + "\nDo not run; final condition: preserve phase"
        text, ref = self.pending(body)
        transfer_before = peer_message._read_transfer_record(ref)
        digest_before = transfer_before["body_sha256"]
        with mock.patch.object(peer_message, "_codex_model_read_access", return_value=False), \
             mock.patch.object(queue, "send_at_least_once", return_value={"status": "queued"}) as send:
            result = peer_message.deliver_pending_codex(ref)
        self.assertEqual(result["status"], "queued")
        self.assertEqual(send.call_args.kwargs["message"], text)
        row = peer_message._read_pending(ref)
        self.assertTrue(row["reference_context_fallback"])
        self.assertEqual(row["text"], text)
        self.assertEqual(row["body_sha256"], digest_before)
        transfer_after = peer_message._read_transfer_record(ref)
        self.assertEqual(transfer_after, transfer_before)
        context = peer_message.receiver_guidance(text, self.recipient)
        self.assertIn(body, context)
        self.assertIn("Do not act on the bounded preview", context)
        self.assertIn("final condition: preserve phase", context)

    def test_exact_native_history_ack_once_after_queued_and_replay(self):
        import codex_queue_delivery as queue
        _text, ref = self.pending()
        with mock.patch.object(queue, "send_at_least_once", return_value={"status": "consumed"}) as send, \
                mock.patch.object(peer_message, "_ack_registered_delivery",
                                  wraps=peer_message._ack_registered_delivery) as repair_ack:
            self.assertEqual(peer_message.deliver_pending_codex(ref)["status"], "received")
            original = self._ledger_file("sender").read_bytes()
            self.assertEqual(peer_message.deliver_pending_codex(ref)["status"], "received")
            self.assertEqual(send.call_count, 1)
            self.assertEqual(repair_ack.call_count, 2)
        self.assertEqual(self._ledger_file("sender").read_bytes(), original)

    def test_actual_native_observer_to_pending_delivery_ack_and_replay(self):
        import codex_queue_delivery as queue
        text, ref = self.pending()
        self.install_native_observation(ref, text)
        with mock.patch.object(queue, "_rpc") as rpc:
            result = peer_message.deliver_pending_codex(ref)
            replay = peer_message.deliver_pending_codex(ref)
        self.assertEqual(result, {"status": "received", "reason": "exact-native-history"})
        self.assertEqual(replay["status"], "received")
        self.assertEqual(rpc.call_count, 0)
        row = peer_message._read_pending(ref)
        self.assertEqual(row["state"], "received")
        self.assertIsNone(row["text"])
        records = [json.loads(line) for line in self._ledger_file("sender").read_text().splitlines()]
        self.assertEqual(sum(ref in record.get("refs", []) for record in records), 1)

    def test_unknown_new_pending_queues_once_then_only_observes(self):
        import codex_queue_delivery as queue
        text, ref = self.pending()
        calls = []

        def rpc(_path, method, _params, *, timeout):
            calls.append(method)
            if method == "thread/queue/list":
                return {"data": [], "nextCursor": None}
            if method == "thread/queue/add":
                return {"queuedSubmission": {"id": "native-item"}}
            self.fail(f"unexpected native RPC: {method}")

        with mock.patch.object(queue, "_rpc", side_effect=rpc):
            first = peer_message.deliver_pending_codex(ref)
            second = peer_message.deliver_pending_codex(ref)
        self.assertEqual(first["status"], "queued")
        self.assertEqual(second["status"], "unverified")
        self.assertEqual(calls, ["thread/queue/list", "thread/queue/add", "thread/queue/list"])
        row = peer_message._read_pending(ref)
        self.assertEqual(row["state"], "unverified")
        self.assertEqual(row["receipt"], "native-queue-accepted")
        self.assertEqual(row["text"], text)
        self.assertFalse(self._ledger_file("sender").exists())

    def test_json5_observation_unknown_uses_exact_claimed_add_once_without_ack(self):
        import codex_queue_delivery as queue
        text, ref = self.pending("json5 parser disagreement")
        client_id = "delivery-peer-" + ref
        item_json = ("{type: 'userMessage', clientId: '" + client_id
                     + "', content: [{type: 'text', text: 'json5 parser disagreement', "
                     + "text_elements: []}]}")
        self.install_native_observation(ref, text, item_json=item_json)
        calls = []

        def rpc(_path, method, params, *, timeout):
            calls.append((method, params))
            if method == "thread/queue/list":
                return {"data": [], "nextCursor": None}
            if method == "thread/queue/add":
                self.assertEqual(params["threadId"], self.recipient["session_id"])
                self.assertEqual(params["clientUserMessageId"], client_id)
                row = peer_message._read_pending(ref)
                self.assertEqual(row["state"], "unverified")
                self.assertEqual(row["receipt"], "native-queue-inflight")
                claim = row.get("rpc_claim")
                self.assertIsInstance(claim, dict)
                self.assertEqual(claim.get("pid"), os.getpid())
                self.assertTrue(claim.get("token"))
                return {"queuedSubmission": {"id": "native-item"}}
            self.fail(f"unexpected native RPC: {method}")

        with mock.patch.object(queue, "_rpc", side_effect=rpc):
            first = peer_message.deliver_pending_codex(ref)
            replay = peer_message.deliver_pending_codex(ref)

        self.assertEqual(first["status"], "queued")
        self.assertEqual(replay["status"], "unverified")
        self.assertEqual([method for method, _params in calls],
                         ["thread/queue/list", "thread/queue/add", "thread/queue/list"])
        self.assertTrue(all(params["threadId"] == self.recipient["session_id"]
                            for _method, params in calls))
        add_params = next(params for method, params in calls if method == "thread/queue/add")
        self.assertEqual(add_params["input"], [{"type": "text", "text": text}])
        self.assertEqual(peer_message._read_pending(ref)["state"], "unverified")
        self.assertEqual(peer_message._read_pending(ref)["text"], text)
        self.assertFalse(self._ledger_file("sender").exists())

    def test_actual_observer_path_reclaims_dead_claim_and_preserves_live_claim_boundary(self):
        import codex_queue_delivery as queue
        from dispatch_contract import process_start_ticks
        _text, ref = self.pending()
        old_claim = {"token": "old-token", "pid": os.getpid(),
                     "start": process_start_ticks(os.getpid())}
        with peer_message.pending_lock(ref):
            peer_message._save_pending(dict(peer_message._read_pending(ref), rpc_claim=old_claim))
        calls = []

        def rpc(_path, method, _params, *, timeout):
            calls.append(method)
            if method == "thread/queue/list":
                return {"data": [], "nextCursor": None}
            if method == "thread/queue/add":
                return {"queuedSubmission": {"id": "native-item"}}
            self.fail(f"unexpected native RPC: {method}")

        with mock.patch("dispatch_contract.process_identity_disposition", return_value="dead"), \
             mock.patch.object(queue, "_rpc", side_effect=rpc):
            result = peer_message.deliver_pending_codex(ref)
        self.assertEqual(result["status"], "queued")
        self.assertEqual(calls, ["thread/queue/list", "thread/queue/add"])
        self.assertEqual(peer_message._read_pending(ref)["state"], "queued")

    def test_actual_observer_publish_failure_keeps_attempt_unverified_without_readd(self):
        import codex_queue_delivery as queue
        _text, ref = self.pending("accepted then unpublishable")
        calls = []

        def rpc(_path, method, _params, *, timeout):
            calls.append(method)
            if method == "thread/queue/list":
                return {"data": [], "nextCursor": None}
            if method == "thread/queue/add":
                return {"queuedSubmission": {"id": "native-item"}}
            self.fail(f"unexpected native RPC: {method}")

        save_pending = peer_message._save_pending

        def fail_queued_publish(row):
            if row["state"] == "queued":
                raise OSError("fixture-publish-failure")
            save_pending(row)

        with mock.patch.object(queue, "_rpc", side_effect=rpc), \
             mock.patch.object(peer_message, "_save_pending", side_effect=fail_queued_publish):
            with self.assertRaisesRegex(OSError, "fixture-publish-failure"):
                peer_message.deliver_pending_codex(ref)
        after_failure = peer_message._read_pending(ref)
        self.assertEqual(after_failure["state"], "unverified")
        self.assertIsInstance(after_failure.get("rpc_claim"), dict)

        with mock.patch("dispatch_contract.process_identity_disposition", return_value="dead"), \
             mock.patch.object(queue, "_rpc", side_effect=rpc):
            replay = peer_message.deliver_pending_codex(ref)
        self.assertEqual(replay["status"], "unverified")
        self.assertEqual(calls, ["thread/queue/list", "thread/queue/add", "thread/queue/list"])
        self.assertEqual(peer_message._read_pending(ref)["state"], "unverified")
        self.assertFalse(self._ledger_file("sender").exists())

    def test_actual_observer_concurrent_claim_other_ref_and_receiver_ack_race(self):
        import codex_queue_delivery as queue
        text, ref = self.pending("first exact peer body")
        _other_text, other_ref = self.pending("second independent peer body")
        entered, release = threading.Event(), threading.Event()
        calls = []
        results = []

        def rpc(_path, method, params, *, timeout):
            client_id = params.get("clientUserMessageId")
            calls.append((method, client_id))
            if method == "thread/queue/list":
                return {"data": [], "nextCursor": None}
            if method == "thread/queue/add":
                if client_id == "delivery-peer-" + ref:
                    entered.set()
                    self.assertTrue(release.wait(3))
                return {"queuedSubmission": {"id": "native-item-" + client_id[-4:]}}
            self.fail(f"unexpected native RPC: {method}")

        worker = threading.Thread(
            target=lambda: results.append(peer_message.deliver_pending_codex(ref)))
        with mock.patch.object(queue, "_rpc", side_effect=rpc):
            worker.start()
            try:
                self.assertTrue(entered.wait(3))
                inflight = peer_message.deliver_pending_codex(ref)
                other = peer_message.deliver_pending_codex(other_ref)
                self.assertEqual(inflight["reason"], "peer-native-inflight")
                self.assertEqual(other["status"], "queued")
                self.assertEqual(peer_message.receive_peer_message(text, self.recipient), 0)
            finally:
                release.set()
                worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(results[0]["status"], "received")
        self.assertEqual(peer_message._read_pending(ref)["state"], "received")
        self.assertIsNone(peer_message._read_pending(ref)["text"])
        self.assertEqual(sum(method == "thread/queue/add" for method, _ in calls), 2)

    def test_stale_or_unsupported_callback_retains_pending(self):
        _text, ref = self.pending()
        row = json.loads(peer_message._pending_path(ref).read_text())
        row["to"]["session_id"] = "stale-old-session"
        peer_message._pending_path(ref).write_text(json.dumps(row))
        with self.assertRaisesRegex(ValueError, "binding-invalid"):
            peer_message.deliver_pending_codex(ref)
        peer_message._pending_path(ref).unlink()
        text, other = peer_message.prepare_peer_message("claude pending", self.sender,
            dict(self.recipient, harness="claude"), defer=True)
        self.assertEqual(peer_message.deliver_pending_codex(other)["status"], "unsupported")
        self.assertEqual(peer_message._read_pending(other)["text"], text)

    def test_render_failure_keeps_unverified_claim_and_cannot_claim_twice(self):
        _text, ref = self.pending()
        args = peer_message.argparse.Namespace(to_harness="codex", to_session_id="recipient", claim=ref, queued=None)
        with mock.patch("builtins.print", side_effect=BrokenPipeError):
            with self.assertRaises(BrokenPipeError):
                peer_message.cmd_pending(args)
        self.assertEqual(peer_message._read_pending(ref)["state"], "unverified")
        with mock.patch("builtins.print") as output:
            self.assertEqual(peer_message.cmd_pending(args), 0)
        output.assert_called_once_with("null")

    def test_ledger_failure_or_publish_failure_does_not_fabricate_receipt_or_duplicate(self):
        text, ref = self.pending()
        with mock.patch.object(peer_message, "cmd_record", return_value=1):
            self.assertEqual(peer_message.receive_peer_message(text, self.recipient), 1)
        self.assertEqual(peer_message._read_pending(ref)["state"], "pending")
        with mock.patch.object(peer_message, "_save_pending", side_effect=OSError("publish failed")):
            with self.assertRaises(OSError):
                peer_message.receive_peer_message(text, self.recipient)
        original = self._ledger_file("sender").read_bytes()
        peer_message.receive_peer_message(text, self.recipient)
        self.assertEqual(self._ledger_file("sender").read_bytes(), original)

    def test_crash_after_native_acceptance_cannot_repeat_add_when_history_is_lost(self):
        import codex_queue_delivery as queue
        text, ref = self.pending()
        def accepted(*args, **kwargs):
            kwargs["before_add"]()
            # Simulate accepted API response followed by failed state publication.
            raise OSError("state-publish-after-acceptance")
        with mock.patch.object(queue, "send_at_least_once", side_effect=accepted):
            self.assertEqual(peer_message.deliver_pending_codex(ref)["status"], "unverified")
        self.assertEqual(peer_message._read_pending(ref)["state"], "unverified")
        self.assertEqual(peer_message._read_pending(ref)["text"], text)
        with mock.patch.object(queue, "send_at_least_once", return_value={"status": "queued"}) as send:
            peer_message.deliver_pending_codex(ref)
        self.assertFalse(send.call_args.kwargs["allow_add"])

    def test_unknown_pending_retry_reuses_original_ref_without_attaching_new_sid(self):
        unknown = {"harness": "opencode", "session_id": None, "name": "original-name"}
        text, ref = peer_message.prepare_peer_message("unknown peer", self.sender, unknown, defer=True)
        self.assertEqual(peer_message.prepare_peer_message("unknown peer", self.sender, unknown, defer=True), (text, ref))
        self.assertEqual(peer_message._read_pending(ref)["state"], "unverified")
        self.assertEqual(peer_message.pending_messages(dict(unknown, session_id="new-fork")), [])
        different = dict(unknown, name="different-name")
        self.assertNotEqual(peer_message.prepare_peer_message("unknown peer", self.sender, different, defer=True)[1], ref)

    def test_two_recipient_rpc_and_receive_race_do_not_block_or_resurrect_payload(self):
        import codex_queue_delivery as queue
        text, ref = self.pending()
        entered, finish = threading.Event(), threading.Event()
        results = []
        def rpc(*args, **kwargs):
            if kwargs["client_message_id"] == "delivery-peer-" + ref:
                entered.set()
                self.assertTrue(finish.wait(3))
            return {"status": "queued"}
        with mock.patch.object(queue, "send_at_least_once", side_effect=rpc) as send:
            worker = threading.Thread(target=lambda: results.append(peer_message.deliver_pending_codex(ref)))
            worker.start()
            try:
                self.assertTrue(entered.wait(3))
                self.assertEqual(peer_message.deliver_pending_codex(ref)["reason"], "peer-native-inflight")
                other = dict(self.recipient, session_id="other-live-rpc")
                ordinary = peer_message.prepare_peer_message("ordinary peer", self.sender, other)
                deferred = peer_message.prepare_peer_message("deferred peer", self.sender, other, defer=True)
                self.assertTrue(ordinary[0].startswith("ordinary peer"))
                self.assertEqual(peer_message.deliver_pending_codex(deferred[1])["status"], "queued")
                # Actual receiver acknowledgment while the RPC is inflight wins
                # the compare-and-publish; the later queued result cannot undo it.
                peer_message.receive_peer_message(text, self.recipient)
            finally:
                finish.set(); worker.join(3)
            self.assertFalse(worker.is_alive())
            self.assertEqual(send.call_count, 2)
        self.assertEqual(results[0]["status"], "received")
        self.assertIsNone(peer_message._read_pending(ref)["text"])

    def test_changed_claim_after_rpc_is_not_overwritten(self):
        import codex_queue_delivery as queue
        text, ref = self.pending()
        def changed(*args, **kwargs):
            with peer_message.pending_lock(ref):
                row = peer_message._read_pending(ref)
                peer_message._save_pending(dict(row, rpc_claim=dict(row["rpc_claim"], token="different")))
            return {"status": "queued"}
        with mock.patch.object(queue, "send_at_least_once", side_effect=changed):
            self.assertEqual(peer_message.deliver_pending_codex(ref)["reason"], "peer-claim-changed-after-rpc")
        row = peer_message._read_pending(ref)
        self.assertEqual(row["rpc_claim"]["token"], "different")
        self.assertEqual(row["state"], "pending")
        self.assertEqual(row["text"], text)


class HandoffAcceptanceTest(_TmpRootMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        import session_tidy_runner as runner
        import session_tidy
        from types import SimpleNamespace
        self.runner = runner
        self.steward = runner._handoff_steward()
        self.steward.peer_message = peer_message
        self.sender = {"harness": "claude", "session_id": "predecessor", "name": "old"}
        self.recipient = {"harness": "codex", "session_id": "successor", "name": "new"}
        self.predecessor = {**self.sender, "pane": "p1", "server": "default", "foreground": {"pid": 11, "start": "101"}}
        self.successor = {**self.recipient, "pane": "p2", "server": "default", "foreground": {"pid": 22, "start": "202"}}
        self.caller = (session_tidy.Seat("pane", "seat", "p2"), "codex", "successor")
        self.text, self.ref = peer_message.prepare_peer_message("[handoff] card", self.sender, self.recipient, defer=True)
        row = peer_message._read_pending(self.ref)
        row["handoff"] = {"target": "p1", "predecessor": self.predecessor, "successor": self.successor,
            "requester": {"harness": "codex", "session_id": "successor", "pane": "p2", "server": "default"}}
        peer_message._save_pending(row)
        self.store = self.steward.peer_obligations.ObligationStore(self.jobs_path.parent)
        patches = [mock.patch.object(runner, "_handoff_steward", return_value=self.steward),
            mock.patch.object(self.steward, "_retire_target", return_value=("idle", {k:v for k,v in self.successor.items() if k not in {"foreground", "server"}}, None, None)),
            mock.patch.object(self.steward, "_retire_foreground", return_value=self.successor["foreground"]),
            mock.patch.object(self.steward, "_pid_identity_ok", return_value=True),
            mock.patch.object(self.steward.peer_obligations, "ObligationStore", return_value=self.store),
            mock.patch.object(self.steward.peer_obligations, "ensure_normal_runner"),
            mock.patch.object(self.steward, "_HERDR_SESSION", None)]
        for p in patches:
            p.start(); self.addCleanup(p.stop)

    def legacy_reference(self, body, sender, recipient):
        evidence = {"schema": "legacy-reference-fixture", "recipient": dict(recipient)}
        with mock.patch.object(peer_message, "_reference_access_evidence_valid", return_value=True):
            return peer_message.prepare_peer_message(
                body, sender, recipient, defer=True, reference_evidence=evidence)

    def test_ordinary_sealed_message_skips_runtime_handoff_resolution(self):
        import session_tidy
        text, ref = peer_message.prepare_peer_message(
            "Please review this ordinary request", self.sender, self.recipient, defer=True)
        path = peer_message._pending_path(ref)
        before_bytes = path.read_bytes()
        before_obligations = self.store.list()
        self.assertEqual(peer_message.parse_peer_trailer(
            text, self.recipient, include_ref=True)["transfer_ref"], ref)
        self.assertNotIn("handoff", peer_message._read_pending(ref))
        self.assertNotIn("acceptance", peer_message._read_pending(ref))

        with mock.patch.object(session_tidy, "resolve_caller") as resolve, \
             mock.patch.object(self.steward, "_retire_target") as target:
            guidance = peer_message.receiver_guidance(
                text, self.recipient, cwd=str(self.tmp_root))

        self.assertEqual(guidance, peer_message._ordinary_receiver_guidance())
        self.assertNotIn("handoff accept", guidance)
        resolve.assert_not_called()
        target.assert_not_called()
        self.assertEqual(path.read_bytes(), before_bytes)
        self.assertNotIn("acceptance", peer_message._read_pending(ref))
        self.assertEqual(self.store.list(), before_obligations)

    def test_native_wrapper_without_exact_ref_skips_runtime_handoff_resolution(self):
        import session_tidy
        wrapped = ('<pasted_content id="native-42">\n'
                   'ordinary native notification\n</pasted_content>')
        parsed = peer_message.parse_peer_trailer(wrapped, self.recipient, include_ref=True)
        self.assertFalse(parsed and parsed.get("transfer_ref"))
        before_obligations = self.store.list()

        with mock.patch.object(session_tidy, "resolve_caller") as resolve, \
             mock.patch.object(self.steward, "_retire_target") as target:
            guidance = peer_message.receiver_guidance(
                wrapped, self.recipient, cwd=str(self.tmp_root))

        self.assertEqual(guidance, peer_message._ordinary_receiver_guidance())
        self.assertNotIn("handoff accept", guidance)
        resolve.assert_not_called()
        target.assert_not_called()
        self.assertEqual(self.store.list(), before_obligations)

    def test_receiver_guidance_command_binds_each_adapter_identity(self):
        import session_tidy
        for harness in ("claude", "codex", "opencode"):
            with self.subTest(harness=harness):
                recipient = {"harness": harness, "session_id": harness + "-receiver", "name": "new"}
                card = "[handoff] card\n" + "referenced document and predecessor details " * 120
                text, ref = self.legacy_reference(card, self.sender, recipient)
                predecessor = {**self.predecessor}
                successor = {**recipient, "pane": "p2", "server": "default",
                             "foreground": {"pid": 22, "start": "202"}}
                row = peer_message._read_pending(ref)
                row["handoff"] = {"card_id": "card-exact", "target": "p1",
                    "predecessor": predecessor, "successor": successor,
                    "requester": {"harness": harness, "session_id": recipient["session_id"],
                                  "pane": "p2", "server": "default"}}
                peer_message._save_pending(row)
                caller = (session_tidy.Seat("pane", "seat-" + harness, "p2"),
                          harness, recipient["session_id"])
                current = {key: value for key, value in successor.items()
                           if key not in {"server", "foreground"}}
                with mock.patch.object(session_tidy, "resolve_caller", return_value=caller), \
                     mock.patch.object(self.steward, "_retire_target",
                                       return_value=("idle", current, None, None)):
                    guidance = peer_message.receiver_guidance(text, recipient, cwd=str(self.tmp_root))
                self.assertEqual(guidance.count("handoff accept "), 1)
                command = guidance.split("run exactly:\n`", 1)[1].split("`", 1)[0]
                tokens = shlex.split(command)
                self.assertEqual(tokens[:3], ["python3", "$AGENT_HOME/utilities/session_tidy.py", "handoff"])
                args = session_tidy.build_parser().parse_args(tokens[2:])
                self.assertEqual(args.command, "handoff")
                self.assertEqual(args.target, "accept")
                self.assertEqual(args.transfer_ref, ref)
                self.assertEqual(args.harness, harness)
                self.assertEqual(args.session_id, recipient["session_id"])
                self.assertEqual(args.cwd, str(self.tmp_root.resolve()))
                after = peer_message._read_pending(ref)
                self.assertNotIn("acceptance", after)
                if harness == "codex":
                    self.assertIn("Open the complete referenced file before acting", guidance)
                else:
                    self.assertIn(card, guidance)
                    self.assertIn("Do not act on the bounded preview", guidance)
                self.assertEqual(peer_message.read_peer_payload(ref)["body"], card)
        self.assertEqual(self.store.list(), [])

    def test_received_scrubbed_handoff_keeps_exact_guidance_without_accepting(self):
        import session_tidy
        peer_message.receive_peer_message(self.text, self.recipient)
        received = peer_message._read_pending(self.ref)
        self.assertEqual(received["state"], "received")
        self.assertIsNone(received["text"])
        caller = self.caller
        with mock.patch.object(session_tidy, "resolve_caller", return_value=caller):
            guidance = peer_message.receiver_guidance(self.text, self.recipient, cwd=str(self.tmp_root))
        self.assertIn(self.ref, guidance)
        self.assertIn("--harness codex --session-id successor", guidance)
        self.assertNotIn("acceptance", peer_message._read_pending(self.ref))
        self.assertEqual(self.store.list(), [])

    def test_guidance_suppresses_project_wrong_and_unverified_receivers(self):
        import session_tidy
        project_caller = (session_tidy.Seat("project", "project-seat", ""), "codex", "successor")
        with mock.patch.object(session_tidy, "resolve_caller", return_value=project_caller), \
             mock.patch.object(self.steward, "_retire_target") as target:
            guidance = peer_message.receiver_guidance(self.text, self.recipient, cwd=str(self.tmp_root))
        self.assertNotIn("handoff accept", guidance)
        target.assert_not_called()

        wrong_caller = (self.caller[0], "codex", "other-session")
        with mock.patch.object(session_tidy, "resolve_caller", return_value=wrong_caller):
            guidance = peer_message.receiver_guidance(self.text, self.recipient, cwd=str(self.tmp_root))
        self.assertNotIn("handoff accept", guidance)

        with mock.patch.object(session_tidy, "resolve_caller", return_value=self.caller), \
             mock.patch.object(self.steward, "_pid_identity_ok", return_value=False):
            guidance = peer_message.receiver_guidance(self.text, self.recipient, cwd=str(self.tmp_root))
        self.assertNotIn("handoff accept", guidance)
        self.assertNotIn("acceptance", peer_message._read_pending(self.ref))
        self.assertEqual(self.store.list(), [])

    def test_guidance_needs_exact_sealed_ref_and_suppresses_partial_acceptance(self):
        import session_tidy
        ordinary = "Please continue from this card; [handoff] is only text."
        for text, recipient in (
            (ordinary + " (peer-from: codex [0a] tag ; ref=11111111111111111111111111111111)", self.recipient),
            (self.text, {"harness": "codex", "session_id": "foreign"}),
            (self.text, {"harness": "codex", "session_id": None}),
        ):
            guidance = peer_message.receiver_guidance(text, recipient, cwd=str(self.tmp_root))
            self.assertNotIn("handoff accept", guidance)
        self.assertNotIn("handoff accept", peer_message.receiver_guidance())

        row = peer_message._read_pending(self.ref)
        row["acceptance"] = {"read_and_accepted": True, "settled": False}
        peer_message._save_pending(row)
        with mock.patch.object(session_tidy, "resolve_caller", side_effect=AssertionError("already accepted")):
            guidance = peer_message.receiver_guidance(self.text, self.recipient, cwd=str(self.tmp_root))
        self.assertNotIn("handoff accept", guidance)
        self.assertEqual(peer_message._read_pending(self.ref)["acceptance"],
                         {"read_and_accepted": True, "settled": False})

    def test_receipt_never_accepts_and_explicit_acceptance_is_once_only(self):
        peer_message.receive_peer_message(self.text, self.recipient)
        self.assertNotIn("acceptance", peer_message._read_pending(self.ref))
        self.assertEqual(self.store.list(), [])
        self.runner.accept_handoff(self.ref, self.caller)
        first = peer_message._read_pending(self.ref)["acceptance"]
        reply = peer_message._read_pending(first["reply_ref"])
        peer_message.receive_peer_message(reply["text"], self.sender)
        self.runner.accept_handoff(self.ref, self.caller)
        self.assertEqual(peer_message._read_pending(self.ref)["acceptance"], first)
        self.assertEqual(len([d for d in self.store.list() if d["intent"]["kind"] == "retire"]), 1)
        self.assertEqual(self.store.get(first["retire_id"])["intent"]["requester"]["session_id"], "successor")
        self.assertEqual(peer_message._read_pending(first["reply_ref"])["state"], "received")

    def test_crash_after_reply_and_after_reservation_reuses_both(self):
        reserve = self.steward.reserve_handoff_retire
        for after in (False, True):
            def crash(*args):
                if after:
                    reserve(*args)
                raise RuntimeError("crash")
            with mock.patch.object(self.steward, "reserve_handoff_retire", side_effect=crash):
                with self.assertRaises(RuntimeError):
                    self.runner.accept_handoff(self.ref, self.caller)
            reply_ref = peer_message._read_pending(self.ref)["acceptance"]["reply_ref"]
            duty = next(d for d in self.store.list(states={"pending", "unknown", "complete"}) if d["intent"].get("handoff_acceptance"))
            self.steward._resume_message_obligation(duty, self.store)
            self.assertEqual(self.store.get(duty["id"])["state"], "complete")
            self.assertEqual(peer_message._read_pending(self.ref)["acceptance"]["reply_ref"], reply_ref)
            self.assertEqual(len([d for d in self.store.list() if d["intent"]["kind"] == "retire"]), 1)

    def test_reincarnated_successor_and_foreign_receiver_cannot_accept(self):
        with mock.patch.object(self.steward, "_retire_foreground", return_value={"pid": 22, "start": "changed"}):
            with self.assertRaises(ValueError):
                self.runner.accept_handoff(self.ref, self.caller)
        with self.assertRaises(ValueError):
            self.runner.accept_handoff(self.ref, (self.caller[0], "codex", "supervisor"))
        self.assertNotIn("acceptance", peer_message._read_pending(self.ref))
        self.assertEqual(self.store.list(), [])

    def test_normal_handoff_binds_card_and_lifetimes_before_delivery_without_acceptance(self):
        import session_tidy as tidy
        from types import SimpleNamespace
        def target(name):
            identity = self.predecessor if name == "p1" else self.successor
            return "idle", {k:v for k,v in identity.items() if k not in {"foreground", "server"}}, None, None
        refs = []
        outcome, exit_code = getattr(self, "handoff_outcome", ("queued", 3))
        def send(args, *, expected_recipient, on_prepared):
            self.assertEqual(expected_recipient, ("codex", "successor"))
            body = Path(args.body_file).read_text()
            text, ref = peer_message.prepare_peer_message(body, self.sender, self.recipient, defer=True)
            on_prepared(ref)
            refs.append(ref)
            row = peer_message._read_pending(ref)
            self.assertEqual(row["handoff"]["card_id"], "card-exact")
            self.assertEqual(row["handoff"]["predecessor"], self.predecessor)
            self.assertEqual(row["handoff"]["successor"], self.successor)
            self.assertNotIn("acceptance", row)
            print(f"prompted={outcome} reason=native-queue")
            return exit_code
        with mock.patch.object(tidy, "read_latest_card", return_value={"id": "card-exact", "body": "read this card"}), \
             mock.patch.object(tidy, "state_root", return_value=self.tmp_root), \
             mock.patch.object(tidy, "mark_card_handed_off") as marked, \
             mock.patch.object(self.runner, "injected_executable", return_value=None), \
             mock.patch.object(self.steward, "_retire_target", side_effect=target), \
             mock.patch.object(self.steward, "_retire_foreground", side_effect=lambda pane, *a, **kw: (self.predecessor if pane == "p1" else self.successor)["foreground"]), \
             mock.patch.object(self.steward, "_current_session_identity", return_value=("predecessor", "claude")), \
             mock.patch.object(self.steward, "cmd_prompt", side_effect=send):
            line, code = self.runner.handoff(tidy.Seat("pane", "old-seat", "p1"), "p2")
        self.assertEqual(code, exit_code)
        self.assertIn(f"prompted={outcome}", line)
        marked.assert_called_once()
        self.assertEqual(self.store.list(), [])

    def test_unverified_send_keeps_exact_retained_handoff(self):
        self.handoff_outcome = ("unverified", 5)
        self.test_normal_handoff_binds_card_and_lifetimes_before_delivery_without_acceptance()

    def test_acceptance_reservation_uses_existing_cancelled_retry_lineage(self):
        request = peer_message._read_pending(self.ref)["handoff"]["requester"]
        original_id = self.steward.peer_obligations.stable_duty_id("retire", self.predecessor, "p1")
        self.store.create(original_id, "retire", self.predecessor, {"target": "p1", "requester": request})
        self.store.update(original_id, state="cancelled", cleanup="complete")
        retry = self.steward.reserve_handoff_retire("p1", self.predecessor, request)
        self.assertNotEqual(retry, original_id)
        self.assertEqual(self.store.get(retry)["intent"]["retry_of"], original_id)
        self.assertEqual(self.store.get(retry)["intent"]["requester"], request)
        self.assertEqual(self.steward.reserve_handoff_retire("p1", self.predecessor, request), retry)
        self.assertEqual(self.store.get(original_id)["state"], "cancelled")

    def test_original_retire_request_is_preserved_with_unknown_current_foreground(self):
        request = peer_message._read_pending(self.ref)["handoff"]["requester"]
        original = self.store.create("retire-c375ee7af4e37ec3085239d1b7bd98db", "retire", self.predecessor,
                                     {"target": "p1", "requester": request})
        unknown = dict(self.predecessor, foreground=None)
        reserved = self.steward.reserve_handoff_retire("p1", unknown, request)
        self.assertEqual(reserved, original["id"])
        self.assertEqual(self.store.get(reserved)["intent"], original["intent"])
        with self.assertRaises(ValueError):
            self.steward.reserve_handoff_retire("p1", unknown, dict(request, session_id="supervisor"))

    def test_concurrent_duplicate_keeps_one_reply_and_reservation(self):
        barrier = threading.Barrier(2)
        results = []
        def accept():
            barrier.wait()
            try:
                results.append(self.runner.accept_handoff(self.ref, self.caller)[1])
            except BlockingIOError:
                results.append("busy")
        workers = [threading.Thread(target=accept) for _ in range(2)]
        for w in workers: w.start()
        for w in workers: w.join()
        self.runner.accept_handoff(self.ref, self.caller)
        self.assertIn(0, results)
        self.assertEqual(len([d for d in self.store.list() if d["intent"]["kind"] == "retire"]), 1)

    def _assert_acceptance_recovers(self):
        duty = next(d for d in self.store.list() if d["intent"].get("handoff_acceptance"))
        self.steward._resume_message_obligation(duty, self.store)
        accepted = peer_message._read_pending(self.ref)["acceptance"]
        self.assertTrue(accepted["settled"])
        self.assertEqual(self.store.get(duty["id"])["state"], "complete")
        self.assertEqual(len([d for d in self.store.list() if d["intent"]["kind"] == "retire"]), 1)
        reply = peer_message._read_pending(accepted["reply_ref"])
        peer_message.receive_peer_message(reply["text"], self.sender)
        self.runner.accept_handoff(self.ref, self.caller)
        self.assertEqual(peer_message._read_pending(accepted["reply_ref"])["state"], "received")
        self.assertEqual(peer_message._read_pending(self.ref)["acceptance"], accepted)

    def test_reply_preparation_interruption_does_not_publish_poisoned_slot(self):
        reply_ref = hashlib.sha256(("handoff-accept:" + self.ref).encode()).hexdigest()[:32]
        dump = peer_message.json.dump
        def interrupted(record, *args, **kwargs):
            if record.get("message_id") == reply_ref:
                raise RuntimeError("interrupted-write")
            return dump(record, *args, **kwargs)
        with mock.patch.object(peer_message.json, "dump", side_effect=interrupted):
            with self.assertRaises(RuntimeError):
                self.runner.accept_handoff(self.ref, self.caller)
        self.assertFalse(peer_message._transfer_path(reply_ref).exists())
        self.assertFalse(list(peer_message._transfer_path(reply_ref).parent.glob(".handoff-reply-*")))
        self._assert_acceptance_recovers()

    def test_complete_reply_publication_before_pending_row_recovers(self):
        reply_ref = hashlib.sha256(("handoff-accept:" + self.ref).encode()).hexdigest()[:32]
        save = peer_message._save_pending
        def interrupted(row):
            if row["ref"] == reply_ref:
                raise RuntimeError("before-pending-row")
            return save(row)
        with mock.patch.object(peer_message, "_save_pending", side_effect=interrupted):
            with self.assertRaises(RuntimeError):
                self.runner.accept_handoff(self.ref, self.caller)
        published = peer_message._transfer_path(reply_ref).read_bytes()
        self.assertEqual(json.loads(published)["message_id"], reply_ref)
        self.assertIsNone(peer_message._read_pending(reply_ref))
        self._assert_acceptance_recovers()
        self.assertEqual(peer_message._transfer_path(reply_ref).read_bytes(), published)

    def test_reply_courier_registration_interruption_recovers(self):
        schedule = self.steward._schedule_message_obligation
        def interrupted(row, *args, **kwargs):
            if row["ref"] != self.ref:
                raise RuntimeError("before-reply-courier")
            return schedule(row, *args, **kwargs)
        with mock.patch.object(self.steward, "_schedule_message_obligation", side_effect=interrupted):
            with self.assertRaises(RuntimeError):
                self.runner.accept_handoff(self.ref, self.caller)
        self._assert_acceptance_recovers()

    def test_conflicting_existing_reply_is_never_replaced(self):
        ref = peer_message.schedule_acceptance_reply(self.ref, self.recipient, self.sender)
        path = peer_message._transfer_path(ref)
        before = path.read_bytes()
        with self.assertRaisesRegex(ValueError, "reply-conflict"):
            peer_message.schedule_acceptance_reply(self.ref, dict(self.recipient, session_id="foreign"), self.sender)
        self.assertEqual(path.read_bytes(), before)

    def test_unverified_successor_lifetimes_cannot_accept(self):
        original = peer_message._read_pending(self.ref)
        known = self.successor["foreground"]
        cases = [(None, None), (known, None), ({"pid": 22}, known),
                 ({"pid": True, "start": "202"}, known),
                 ({"pid": 22, "start": "invalid"}, known),
                 (known, {"pid": 22, "start": 0})]
        for recorded, current in cases:
            with self.subTest(recorded=recorded, current=current):
                row = json.loads(json.dumps(original))
                row["handoff"]["successor"]["foreground"] = recorded
                peer_message._save_pending(row)
                with mock.patch.object(self.steward, "_retire_foreground", return_value=current):
                    with self.assertRaisesRegex(ValueError, "lifetime-unverified"):
                        self.runner.accept_handoff(self.ref, self.caller)
                self.assertNotIn("acceptance", peer_message._read_pending(self.ref))
                self.assertEqual(self.store.list(), [])
        peer_message._save_pending(original)
        with mock.patch.object(self.steward, "_pid_identity_ok", return_value=False):
            with self.assertRaisesRegex(ValueError, "lifetime-unverified"):
                self.runner.accept_handoff(self.ref, self.caller)
        self.assertEqual(self.store.list(), [])
        self.runner.accept_handoff(self.ref, self.caller)
        self.assertTrue(peer_message._read_pending(self.ref)["acceptance"]["settled"])

    def test_unknown_fresh_predecessor_never_books_a_new_lifetime(self):
        requester = peer_message._read_pending(self.ref)["handoff"]["requester"]
        with self.assertRaisesRegex(ValueError, "predecessor-lifetime-unverified"):
            self.steward.reserve_handoff_retire("p1", dict(self.predecessor, foreground=None), requester)
        self.assertEqual(self.store.list(), [])

    def test_presend_refusals_do_not_consume_card(self):
        import session_tidy as tidy
        import subprocess
        def target(name):
            ident = self.predecessor if name == "p1" else self.successor
            return "idle", {k:v for k,v in ident.items() if k not in {"foreground", "server"}}, None, None
        for injected in (None, Path("/isolated/fake-steward")):
            for verdict in ("prompted=false reason=target-identity-changed",
                            "prompted=unverified reason=peer-pending-or-transfer-unavailable"):
                with self.subTest(injected=injected, verdict=verdict):
                    def refused(*args, **kwargs):
                        print(verdict)
                        return 5
                    with mock.patch.object(tidy, "read_latest_card", return_value={"id":"card", "body":"continue me"}), \
                         mock.patch.object(tidy, "state_root", return_value=self.tmp_root), \
                         mock.patch.object(tidy, "mark_card_handed_off") as marked, \
                         mock.patch.object(self.runner, "injected_executable", return_value=injected), \
                         mock.patch.object(self.steward, "_retire_target", side_effect=target), \
                         mock.patch.object(self.steward, "_current_session_identity", return_value=("predecessor", "claude")), \
                         mock.patch.object(self.steward, "cmd_prompt", side_effect=refused), \
                         mock.patch.object(self.runner.subprocess, "run", return_value=subprocess.CompletedProcess([],5,verdict,"")):
                        line, code = self.runner.handoff(tidy.Seat("pane","old","p1"), "p2")
                    self.assertEqual(code, 5)
                    marked.assert_not_called()
                    self.assertIn(verdict, line)


if __name__ == "__main__":
    unittest.main()
