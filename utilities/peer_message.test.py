#!/usr/bin/env python3
"""Unit tests for utilities/peer-message.py (SD-122 peer-message ledger)."""
import hashlib
import importlib.util
import json
import os
import stat
import sys
import tempfile
import unittest
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
        # steward-wait collision reproduced in evidence/peer-steward-wait.txt.
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
        root = peer_message._ledger_root() / "peer-messages"
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


if __name__ == "__main__":
    unittest.main()
