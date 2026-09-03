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



class F100cSenderAndStewardTest(unittest.TestCase):
    """F-100c — from.name, the herdr sender trailer, and the steward marker."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self._old = dict(os.environ)
        os.environ["AGENT_DISPATCH_JOBS"] = str(self.root / "jobs.log")
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
        for f in (peer_message._ledger_root() / "peer-messages").rglob("*.jsonl"):
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
        self.assertEqual(t, "(peer-from: claude sid-a hearting-46)")
        self.assertEqual(peer_message.parse_peer_trailer("body\n\n" + t),
                         {"harness": "claude", "session_id": "sid-a", "name": "hearting-46"})
        self.assertEqual(peer_message.parse_peer_trailer("(peer-from: codex 01a0)")["name"], None)
        two = "(peer-from: codex one)\nx\n(peer-from: claude two n)"
        self.assertEqual(peer_message.parse_peer_trailer(two)["session_id"], "two")
        self.assertIsNone(peer_message.parse_peer_trailer("no trailer here"))
        self.assertIsNone(peer_message.parse_peer_trailer(None))
        # a name with a ')' cannot break the envelope
        self.assertEqual(peer_message.parse_peer_trailer(
            peer_message.peer_trailer("claude", "s", "a) b"))["name"], "a  b".replace("  ", " "))

    def test_sent_steer_marks_the_sender_as_steward_notice_does_not(self):
        self.assertEqual(peer_message.cmd_record(self._ns()), 0)
        markers = peer_message.read_steward_markers()
        self.assertEqual(set(markers), {("claude", "sid-a")})
        self.assertEqual(markers[("claude", "sid-a")]["targets"]["sid-c"]["harness"], "codex")
        self.assertEqual(peer_message.cmd_record(self._ns(
            from_harness="codex", from_session_id="sid-c", to_harness="claude",
            to_session_id="sid-a", kind="notice", status="received")), 0)
        self.assertEqual(set(peer_message.read_steward_markers()), {("claude", "sid-a")})
        self.assertEqual(peer_message.cmd_release(peer_message.argparse.Namespace(
            harness="claude", session_id="sid-a")), 0)
        self.assertEqual(peer_message.read_steward_markers(), {})

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
        self.assertIn("(peer-from: claude -", text)
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


if __name__ == "__main__":
    unittest.main()
