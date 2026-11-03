#!/usr/bin/env python3
"""Recipient-index and Stop-header contracts for the peer-message Stop path.

The Stop hook must answer "what is pending for this exact session?" without
opening every retained row, and it must draw one pane header without listing
every live agent. These tests pin:

* index accuracy against the existing full scan (same rows, same order);
* a current index means a recipient-only scan opens only its own rows;
* every other call site (strict intent reuse, sender-or-recipient, full scans)
  keeps the existing full scan;
* partial, corrupt and stale indexes are never current;
* the one-shot write hook keeps an existing recipient index current;
* `retry_receiver_idle` reuses the caller's `may_report` answer and loads the
  steward module once;
* the real Stop hook reports the header with one `may_report` call, one
  `read_markers`, no `_projection_sessions`, and reaches the idle callback.
"""
import contextlib
import fcntl
import importlib.util
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

WORKTREE = Path(__file__).resolve().parent.parent
_TOOLS = str(WORKTREE / "tools")
_UTILITIES = str(WORKTREE / "utilities")
for _path in (_TOOLS, _UTILITIES):
    if _path not in sys.path:
        sys.path.insert(0, _path)


def _load_peer_message():
    spec = importlib.util.spec_from_file_location(
        "peer_message_index_subject", WORKTREE / "utilities" / "peer-message.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


peer_message = _load_peer_message()

from fleet import herdr_projection  # noqa: E402
from fleet.collectors import steward  # noqa: E402


class _OpenMeter:
    """Count successful and refused opens issued inside one call."""

    def __init__(self):
        self.calls = 0
        self._os_open = None
        self._builtin_open = None

    def __enter__(self):
        import builtins
        self._os_open = os.open
        self._builtin_open = builtins.open

        def counting_os_open(*args, **kwargs):
            self.calls += 1
            return self._os_open(*args, **kwargs)

        def counting_open(*args, **kwargs):
            self.calls += 1
            return self._builtin_open(*args, **kwargs)

        os.open = counting_os_open
        builtins.open = counting_open
        return self

    def __exit__(self, *_exc):
        import builtins
        os.open = self._os_open
        builtins.open = self._builtin_open
        return False


class IndexTestCase(unittest.TestCase):
    sender = {"harness": "codex", "session_id": "index-sender"}
    recipient = {"harness": "codex", "session_id": "index-recipient"}
    other = {"harness": "claude", "session_id": "index-other"}

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_root = Path(self._tmp.name)
        state = self.tmp_root / "state"
        dispatch = state / "dispatch"
        dispatch.mkdir(parents=True)
        jobs = dispatch / "jobs.log"
        jobs.touch()
        self._old_environ = dict(os.environ)
        for key in tuple(os.environ):
            if (key.startswith(("AGENT_DISPATCH_", "AGENT_ARTIFACT_", "AGENT_RUNTIME_",
                                "AGENT_SESSION_", "AGENT_THREAD_", "HERDR_"))
                    or key in {"AGENT_HOME", "CLAUDE_HOME", "AGENT_SERVICE_CURRENT",
                               "HEARTING_SERVICE_CURRENT", "SERVICE_CURRENT"}):
                os.environ.pop(key, None)
        os.environ["AGENT_DISPATCH_JOBS"] = str(jobs)
        # The production layout keeps the peer ledger and dispatch registry in
        # the same canonical root; the split-root case has its own test below.
        os.environ["AGENT_PEER_LEDGER_ROOT"] = str(dispatch)
        os.environ["HOME"] = str(self.tmp_root / "home")
        os.environ["XDG_STATE_HOME"] = str(state)
        os.environ["XDG_CONFIG_HOME"] = str(self.tmp_root / "xdg-config")
        os.environ["XDG_DATA_HOME"] = str(self.tmp_root / "xdg-data")
        os.environ["HARNESS_STATE_ROOT"] = str(self.tmp_root / "harness-state")
        os.environ["HARNESS_DATA_ROOT"] = str(self.tmp_root / "harness-data")
        os.environ["AGENT_RUNTIME_ROOT"] = str(self.tmp_root / "runtime")
        os.environ["CODEX_HOME"] = str(self.tmp_root / "home" / ".codex")
        os.environ["CLAUDE_CONFIG_DIR"] = str(self.tmp_root / "home" / ".claude")
        os.environ.pop("AGENT_HOME", None)
        self.addCleanup(self._restore_environ)
        self.dispatch_root = dispatch

    def _restore_environ(self):
        os.environ.clear()
        os.environ.update(self._old_environ)

    # -- fixtures ---------------------------------------------------------
    def pending(self, body="index peer body", *, sender=None, recipient=None):
        return peer_message.prepare_peer_message(
            body, sender or self.sender, recipient or self.recipient, defer=True)

    def received_history(self, count):
        pending = peer_message._pending_dir()
        for index in range(count):
            ref = f"{index:032x}"
            path = pending / (ref + ".json")
            path.write_text(json.dumps({"schema_version": 1, "state": "received",
                                        "ref": ref}, sort_keys=True) + "\n",
                            encoding="utf-8")
            path.chmod(0o600)

    def index_path(self, recipient=None):
        recipient = recipient or self.recipient
        return peer_message.recipient_index_path(
            self.dispatch_root, recipient["harness"], recipient["session_id"])

    def scan(self, **kwargs):
        return list(peer_message.pending_messages(kwargs.pop("recipient", self.recipient)))


class RecipientIndexTest(IndexTestCase):
    def test_rebuild_publishes_the_exact_rows_the_full_scan_returns(self):
        _text, first = self.pending("first")
        _text, second = self.pending("second")
        self.received_history(30)
        self.assertFalse(self.index_path().exists())
        full = peer_message.pending_messages(self.recipient)
        self.assertEqual([row["ref"] for row in full], sorted([first, second]))

        result = peer_message.rebuild_recipient_index(self.dispatch_root, time_budget_s=5.0)
        self.assertTrue(result["complete"])
        self.assertEqual(result["recipients"], 1)
        self.assertEqual(result["rows"], 2)

        payload = json.loads(self.index_path().read_text(encoding="utf-8"))
        self.assertEqual(payload["schema"], peer_message._RECIPIENT_INDEX_SCHEMA)
        self.assertEqual(payload["harness"], self.recipient["harness"])
        self.assertEqual(payload["session_id"], self.recipient["session_id"])
        self.assertIs(payload["complete"], True)
        self.assertEqual(payload["file_count"], 32)
        self.assertEqual([row["ref"] for row in payload["rows"]], sorted([first, second]))
        self.assertEqual([row["state"] for row in payload["rows"]], ["pending", "pending"])

        indexed = peer_message.read_recipient_index(
            self.dispatch_root, self.recipient["harness"], self.recipient["session_id"])
        self.assertIsInstance(indexed, list)
        self.assertTrue(indexed.complete)
        self.assertEqual([row["ref"] for row in peer_message.pending_messages(self.recipient)],
                         [row["ref"] for row in full])

    def test_only_recipient_only_non_strict_scan_opens_the_index(self):
        _text, ref = self.pending()
        self.received_history(240)
        with _OpenMeter() as full_meter:
            rows = peer_message.pending_messages(self.recipient)
        self.assertEqual([row["ref"] for row in rows], [ref])
        self.assertGreaterEqual(full_meter.calls, 240)

        with _OpenMeter() as warm_meter:
            rows = peer_message.pending_messages(self.recipient)
        self.assertEqual([row["ref"] for row in rows], [ref])
        # The index, the named row, its transfer record and the directory scan.
        self.assertLessEqual(warm_meter.calls, 15, warm_meter.calls)

    def test_current_index_never_hides_a_temporary_read_failure(self):
        _text, ref = self.pending()
        self.assertEqual([row["ref"] for row in peer_message.pending_messages(self.recipient)],
                         [ref])
        self.assertTrue(self.index_path().exists())
        with mock.patch.object(peer_message, "_read_transfer_record",
                               side_effect=OSError(5, "temporary")):
            self.assertEqual(peer_message.pending_messages(self.recipient), [])
        self.assertEqual([row["ref"] for row in peer_message.pending_messages(self.recipient)],
                         [ref])

    def test_stale_corrupt_and_partial_indexes_fall_back_to_the_full_scan(self):
        _text, ref = self.pending()
        peer_message.rebuild_recipient_index(self.dispatch_root, time_budget_s=5.0)
        self.assertTrue(peer_message.read_recipient_index(
            self.dispatch_root, "codex", "index-recipient").complete)

        # A row that appeared without the write hook stales the recorded name set.
        extra = peer_message._pending_dir() / ("e" * 32 + ".json")
        extra.write_text(json.dumps({"schema_version": 1, "state": "received",
                                     "ref": "e" * 32}) + "\n", encoding="utf-8")
        extra.chmod(0o600)
        self.assertFalse(peer_message.read_recipient_index(
            self.dispatch_root, "codex", "index-recipient").complete)
        self.assertEqual([row["ref"] for row in peer_message.pending_messages(self.recipient)],
                         [ref])
        self.assertTrue(peer_message.read_recipient_index(
            self.dispatch_root, "codex", "index-recipient").complete)

        # Corrupt bytes are not current.
        self.index_path().write_text("{not-json", encoding="utf-8")
        self.assertFalse(peer_message.read_recipient_index(
            self.dispatch_root, "codex", "index-recipient").complete)
        self.assertEqual([row["ref"] for row in peer_message.pending_messages(self.recipient)],
                         [ref])

        # A partial publication is explicitly not current.
        payload = json.loads(self.index_path().read_text(encoding="utf-8"))
        payload["complete"] = False
        self.index_path().write_text(json.dumps(payload), encoding="utf-8")
        self.assertFalse(peer_message.read_recipient_index(
            self.dispatch_root, "codex", "index-recipient").complete)
        self.assertEqual([row["ref"] for row in peer_message.pending_messages(self.recipient)],
                         [ref])

        # A changed row mtime stales the index even when the name set is unchanged.
        row_path = peer_message._pending_path(ref)
        later = row_path.stat().st_mtime_ns + 1
        os.utime(row_path, ns=(later, later))
        self.assertFalse(peer_message.read_recipient_index(
            self.dispatch_root, "codex", "index-recipient").complete)
        self.assertEqual([row["ref"] for row in peer_message.pending_messages(self.recipient)],
                         [ref])

    def test_existing_index_is_updated_by_the_single_write_hook(self):
        _text, first = self.pending("first")
        peer_message.rebuild_recipient_index(self.dispatch_root, time_budget_s=5.0)
        self.assertEqual([row["ref"] for row in peer_message.read_recipient_index(
            self.dispatch_root, "codex", "index-recipient")], [first])

        _text, second = self.pending("second")
        indexed = peer_message.read_recipient_index(
            self.dispatch_root, "codex", "index-recipient")
        self.assertTrue(indexed.complete)
        self.assertEqual([row["ref"] for row in indexed], sorted([first, second]))

        row = peer_message._read_pending(second)
        peer_message._save_pending(dict(row, state="received"))
        indexed = peer_message.read_recipient_index(
            self.dispatch_root, "codex", "index-recipient")
        self.assertTrue(indexed.complete)
        self.assertEqual([row["ref"] for row in indexed], [first])
        self.assertEqual([row["ref"] for row in peer_message.pending_messages(self.recipient)],
                         [first])

    def test_write_hook_does_not_create_an_index(self):
        _text, _ref = self.pending()
        self.assertFalse(self.index_path().exists())

    def test_implicit_publish_never_leaves_the_pending_rows_tree(self):
        peer_root = self.tmp_root / "split-peer"
        other_dispatch = self.tmp_root / "other-dispatch"
        other_dispatch.mkdir()
        with mock.patch.dict(os.environ, {"AGENT_PEER_LEDGER_ROOT": str(peer_root),
                                          "AGENT_DISPATCH_JOBS":
                                              str(other_dispatch / "jobs.log")}):
            _text, ref = self.pending()
            self.assertEqual([row["ref"] for row in peer_message.pending_messages(
                self.recipient)], [ref])
            published = peer_message.recipient_index_path(
                peer_root, "codex", "index-recipient")
            self.assertTrue(published.exists())
            self.assertFalse((other_dispatch / "peer-messages" / "index").exists())
            self.assertTrue(peer_message.read_recipient_index(
                peer_root, "codex", "index-recipient").complete)

    def test_other_recipients_keep_their_own_rows(self):
        _text, mine = self.pending("mine")
        _text, theirs = self.pending("theirs", recipient=self.other)
        peer_message.rebuild_recipient_index(self.dispatch_root, time_budget_s=5.0)
        self.assertEqual([row["ref"] for row in peer_message.pending_messages(self.recipient)],
                         [mine])
        self.assertEqual([row["ref"] for row in peer_message.pending_messages(self.other)],
                         [theirs])
        self.assertEqual(peer_message.read_recipient_index(
            self.dispatch_root, "claude", "not-indexed").complete, False)
        self.assertEqual(peer_message.read_recipient_index(
            self.dispatch_root, "codex", "index-recipient").complete, True)

    def test_rebuild_honors_the_explicit_root_the_runner_observed(self):
        peer_root = self.tmp_root / "split-peer-runner"
        explicit = self.tmp_root / "explicit-root"
        explicit.mkdir()
        with mock.patch.dict(os.environ, {"AGENT_PEER_LEDGER_ROOT": str(peer_root)}):
            _text, _ref = self.pending()
            result = peer_message.rebuild_recipient_index(explicit, time_budget_s=5.0)
            self.assertTrue(result["complete"])
            published = (explicit / "peer-messages" / "index" / "codex"
                         / "index-recipient.json")
            self.assertTrue(published.exists())
            self.assertEqual(list((peer_root / "peer-messages" / "index").glob("**/*.json")), [])
            self.assertTrue(peer_message.read_recipient_index(
                explicit, "codex", "index-recipient").complete)

    def test_a_new_row_for_another_recipient_retires_a_bound_index(self):
        _text, mine = self.pending("mine")
        peer_message.rebuild_recipient_index(self.dispatch_root, time_budget_s=5.0)
        self.assertTrue(peer_message.read_recipient_index(
            self.dispatch_root, "codex", "index-recipient").complete)
        _text, theirs = self.pending("theirs", recipient=self.other)
        # The name set grew in a way this recipient's index did not observe.
        self.assertFalse(peer_message.read_recipient_index(
            self.dispatch_root, "codex", "index-recipient").complete)
        self.assertEqual([row["ref"] for row in peer_message.pending_messages(self.recipient)],
                         [mine])
        self.assertTrue(peer_message.read_recipient_index(
            self.dispatch_root, "codex", "index-recipient").complete)

    def test_budget_and_unreadable_rows_publish_nothing(self):
        _text, _ref = self.pending()
        result = peer_message.rebuild_recipient_index(self.dispatch_root, time_budget_s=0.0)
        self.assertFalse(result["complete"])
        self.assertFalse(self.index_path().exists())

        if os.geteuid() != 0:
            locked = peer_message._pending_dir() / ("a" * 32 + ".json")
            locked.write_text("{}", encoding="utf-8")
            locked.chmod(0)
            try:
                result = peer_message.rebuild_recipient_index(self.dispatch_root,
                                                              time_budget_s=5.0)
                self.assertFalse(result["complete"])
                self.assertFalse(self.index_path().exists())
            finally:
                locked.chmod(0o600)


class CallSitePinTest(IndexTestCase):
    def test_sender_or_recipient_scan_keeps_reading_every_row(self):
        _text, _ref = self.pending()
        self.received_history(120)
        peer_message.rebuild_recipient_index(self.dispatch_root, time_budget_s=5.0)
        with _OpenMeter() as meter:
            list(peer_message._pending_rows(recipient=self.recipient, sender=self.recipient))
        self.assertGreaterEqual(meter.calls, 120)
        with _OpenMeter() as meter:
            list(peer_message._pending_rows(sender=self.recipient))
        self.assertGreaterEqual(meter.calls, 120)

    def test_strict_scan_keeps_raising_instead_of_using_the_index(self):
        _text, _ref = self.pending()
        peer_message.rebuild_recipient_index(self.dispatch_root, time_budget_s=5.0)
        if os.geteuid() == 0:
            self.skipTest("permission denial needs an unprivileged user")
        blocked = peer_message._pending_dir() / ("b" * 32 + ".json")
        blocked.write_text("{}", encoding="utf-8")
        blocked.chmod(0)
        try:
            with self.assertRaises(OSError):
                list(peer_message._pending_rows(recipient=self.recipient, strict=True))
            # The ordinary (non-strict) recipient read stays soft and indexed.
            self.assertEqual(len(peer_message.pending_messages(self.recipient)), 1)
        finally:
            blocked.chmod(0o600)

    def test_idle_callback_still_receives_pending_messages_unchanged(self):
        _text, ref = self.pending()
        full = peer_message.pending_messages(self.recipient)
        peer_message.rebuild_recipient_index(self.dispatch_root, time_budget_s=5.0)
        indexed = peer_message.pending_messages(self.recipient)
        self.assertEqual([row["ref"] for row in indexed], [row["ref"] for row in full])
        self.assertEqual(indexed, full)
        source = (WORKTREE / "utilities" / "peer-steward.py").read_text(encoding="utf-8")
        self.assertIn("rows = peer_message.pending_messages(recipient) if peer else []", source)

    def test_retry_receiver_idle_reuses_the_given_permission(self):
        with mock.patch.dict(os.environ, {"HERDR_PANE_ID": "w1:pPin"}), \
             mock.patch.object(peer_message, "_peer_steward_module") as loaded, \
             mock.patch("fleet.herdr_projection.may_report",
                        side_effect=AssertionError("must not re-walk identity")):
            claude = {"harness": "claude", "session_id": "idle-recipient"}
            peer_message.retry_receiver_idle(claude, permission=False)
            loaded.assert_not_called()
            peer_message.retry_receiver_idle(claude, permission=True)
            loaded.assert_called_once()
            self.assertEqual(loaded.return_value.receiver_idle.call_args.args[0], claude)


class BoundedScanTest(IndexTestCase):
    """What a hook boundary may read: rows naming its endpoints, until its deadline."""

    def _validated(self, call):
        seen = []
        original = peer_message._read_pending

        def counting(ref, **kwargs):
            seen.append(ref)
            return original(ref, **kwargs)

        with mock.patch.object(peer_message, "_read_pending", side_effect=counting):
            rows = list(call())
        return rows, seen

    def test_a_passed_deadline_reads_nothing_and_publishes_no_index(self):
        _text, ref = self.pending()
        self.received_history(20)
        with _OpenMeter() as meter:
            rows = list(peer_message._pending_rows(
                recipient=self.recipient, deadline=time.monotonic() - 1))
        self.assertEqual(rows, [])
        self.assertLess(meter.calls, 5, meter.calls)
        self.assertFalse(self.index_path().exists())
        # The row is untouched, so the next unbounded read still finds it.
        self.assertEqual([row["ref"] for row in peer_message.pending_messages(self.recipient)],
                         [ref])

    def test_filtered_scan_validates_only_rows_naming_an_endpoint(self):
        _text, mine = self.pending("mine")
        _text, sent = self.pending("sent", sender=self.recipient, recipient=self.other)
        foreign = [self.pending(f"foreign {index}", sender=self.other,
                                recipient={"harness": "claude", "session_id": f"far-{index}"})[1]
                   for index in range(5)]
        rows, validated = self._validated(lambda: peer_message._pending_rows(
            recipient=self.recipient, sender=self.recipient))
        self.assertEqual(sorted(row["ref"] for row in rows), sorted([mine, sent]))
        self.assertEqual(sorted(validated), sorted([mine, sent]))
        self.assertTrue(set(foreign).isdisjoint(validated))

    def test_ref_scan_returns_only_rows_carrying_the_ref(self):
        _text, other = self.pending("plain")
        _text, watched = peer_message.prepare_peer_message(
            "[peer-steward] watch_id=abc123 state=idle", self.sender, self.recipient,
            defer=True, refs=["peer-watch:abc123"])
        rows, validated = self._validated(
            lambda: peer_message._pending_rows(ref="peer-watch:abc123"))
        self.assertEqual([row["ref"] for row in rows], [watched])
        self.assertNotIn(other, validated)

    def test_a_write_whose_index_update_never_ran_is_never_hidden(self):
        """RV-BC-1186: a row created without its index update (a writer that
        skipped it, an older release, a crash) must not be certified away by
        the next transition update, which used to republish the current names."""
        _text, first = self.pending("first")
        self.assertEqual([row["ref"] for row in peer_message.pending_messages(self.recipient)],
                         [first])
        self.assertTrue(self.index_path().exists())
        with mock.patch.object(peer_message, "_update_recipient_index"):
            _text, second = self.pending("second")
        row = peer_message._read_pending(first)
        with peer_message.pending_lock(first):
            peer_message._save_pending(dict(row, receipt="later-transition"))
        self.assertEqual(sorted(row["ref"] for row in peer_message.pending_messages(self.recipient)),
                         sorted([first, second]))

    def test_a_row_rebound_to_this_recipient_retires_its_index(self):
        _text, mine = self.pending("mine")
        _text, moved = self.pending("moved", recipient=self.other)
        self.assertEqual([row["ref"] for row in peer_message.pending_messages(self.recipient)],
                         [mine])
        self.assertTrue(self.index_path().exists())
        row = peer_message._read_pending(moved)
        with mock.patch.object(peer_message, "pending_delivery_recipient",
                               return_value=dict(self.recipient)):
            with peer_message.pending_lock(moved):
                peer_message._save_pending(dict(row, receipt="rebound"))
        self.assertFalse(self.index_path().exists())

    def test_the_update_waits_for_the_index_lock_like_every_installed_release(self):
        _text, first = self.pending()
        peer_message.pending_messages(self.recipient)
        path = self.index_path()
        lock = path.with_name(path.name + ".lock")
        fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        waited = []
        original = fcntl.flock

        def observed(handle, operation):
            waited.append(operation)
            if handle != fd and operation == fcntl.LOCK_EX:
                os.close(fd)  # the other writer finishes while this one waits
            return original(handle, operation)

        with mock.patch.object(peer_message.fcntl, "flock", side_effect=observed):
            row = peer_message._read_pending(first)
            with peer_message.pending_lock(first):
                peer_message._save_pending(dict(row, receipt="after-wait"))
        self.assertIn(fcntl.LOCK_EX, waited)

    def test_a_held_index_lock_skips_the_publish_instead_of_waiting(self):
        _text, ref = self.pending()
        path = self.index_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        lock = path.with_name(path.name + ".lock")
        fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            started = time.monotonic()
            rows = peer_message.pending_messages(self.recipient)
            self.assertLess(time.monotonic() - started, 2.0)
            self.assertEqual([row["ref"] for row in rows], [ref])
            self.assertFalse(path.exists())
        finally:
            os.close(fd)
        # Once the lock is free the same read publishes it.
        peer_message.pending_messages(self.recipient)
        self.assertTrue(path.exists())


class StopHeaderTest(IndexTestCase):
    def test_non_steward_header_never_enumerates_every_agent(self):
        with mock.patch.object(steward, "read_markers", return_value={}), \
             mock.patch.object(steward, "read_relations", return_value=[]) as relations, \
             mock.patch("fleet.collectors.herdr._clear_gpu_session_aliases",
                               return_value=[]), \
             mock.patch.object(steward, "_projection_sessions",
                               side_effect=AssertionError("global agent enumeration")):
            self.assertFalse(herdr_projection.is_steward("claude", "quiet-session"))
        # No marker is the restored early return: no relation walk at all.
        relations.assert_not_called()

    def test_possible_steward_still_uses_the_full_role_resolution(self):
        markers = {("codex", "marked"): {
            "harness": "codex", "session_id": "marked",
            "targets": {"child": {"harness": "codex", "session_id": "child",
                                  "kind": "watch", "source": "watch"}},
        }}
        sessions = []
        with mock.patch.object(steward, "read_markers", return_value=markers), \
             mock.patch.object(steward, "read_relations", return_value=[]) as relations, \
             mock.patch("fleet.collectors.herdr._clear_gpu_session_aliases",
                               return_value=[]), \
             mock.patch.object(steward, "_projection_sessions",
                               return_value=sessions) as projection, \
             mock.patch.object(steward, "role_targets", return_value=[]) as targets, \
             mock.patch.object(steward, "role_flag", return_value=True) as flag:
            self.assertTrue(herdr_projection.is_steward("codex", "marked"))
        projection.assert_called_once()
        self.assertIs(targets.call_args.kwargs["_relations"], relations.return_value)
        self.assertIs(flag.call_args.kwargs["_relations"], relations.return_value)


class StopHookSubprocessTest(IndexTestCase):
    """Run the real Stop hook with only `may_report` forced true (test shim)."""

    def _isolation(self):
        root = self.tmp_root / "hook"
        shim = root / "shim"
        bin_dir = root / "bin"
        for directory in (shim, bin_dir):
            directory.mkdir(parents=True)
        audit = root / "audit.jsonl"
        herdr_log = root / "herdr.log"
        fake_herdr = bin_dir / "herdr"
        fake_herdr.write_text(
            "#!/bin/sh\n"
            'printf "%s\\n" "$*" >> "$STOP_HERDR_LOG"\n'
            'case "$1 $2" in\n'
            '  "agent list") printf \'{"result":{"agents":[]}}\' ;;\n'
            '  "agent get") printf \'{"result":{"agent":null}}\' ;;\n'
            'esac\n'
            "exit 0\n", encoding="utf-8")
        fake_herdr.chmod(0o755)
        (shim / "sitecustomize.py").write_text(
            "import json, os, sys\n"
            'sys.path.insert(0, os.environ["STOP_TOOLS_DIR"])\n'
            "import fleet.herdr_projection as _hp\n"
            "import fleet.collectors.steward as _steward\n"
            "def _record(event):\n"
            '    with open(os.environ["STOP_AUDIT_PATH"], "a", encoding="utf-8") as fh:\n'
            "        fh.write(json.dumps({\"event\": event}) + \"\\n\")\n"
            "def _may_report(*args, **kwargs):\n"
            "    _record('may_report')\n"
            "    return True\n"
            "_hp.may_report = _may_report\n"
            "def _wrap(module, name):\n"
            "    original = getattr(module, name)\n"
            "    def wrapper(*args, **kwargs):\n"
            "        _record(name)\n"
            "        return original(*args, **kwargs)\n"
            "    setattr(module, name, wrapper)\n"
            "for _name in ('read_markers', 'read_relations', '_projection_sessions'):\n"
            "    _wrap(_steward, _name)\n", encoding="utf-8")
        return root, shim, audit, herdr_log

    def test_stop_hook_reports_once_and_stays_on_the_per_pane_path(self):
        # One owed row for this exact session makes the idle callback proceed to
        # resolve the pane; the fake herdr then reports no such agent, so nothing
        # is claimed or delivered.
        self.pending("stop body", recipient={"harness": "claude",
                                             "session_id": "stop-session"})
        # One unrelated steward marker keeps the live-read fallback reachable;
        # the published relation file is absent in this fixture.
        marker_dir = self.dispatch_root / "peer-steward" / "claude"
        marker_dir.mkdir(parents=True, exist_ok=True)
        (marker_dir / "other.json").write_text(json.dumps({
            "schema_version": 1, "harness": "claude", "session_id": "other-steward",
            "targets": {"other-steward": {"harness": "claude",
                                          "session_id": "other-steward",
                                          "kind": "watch", "ts": "1"}},
        }), encoding="utf-8")
        root, shim, audit, herdr_log = self._isolation()
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(("AGENT_", "HERDR_"))}
        env.update({
            "HOME": str(self.tmp_root / "home"),
            "XDG_STATE_HOME": str(self.tmp_root / "state"),
            "AGENT_DISPATCH_JOBS": str(self.dispatch_root / "jobs.log"),
            "AGENT_PEER_LEDGER_ROOT": str(self.dispatch_root),
            "AGENT_HOME": str(WORKTREE),
            "AGENT_SESSION_ROLE": "worker",
            "HERDR_PANE_ID": "w1:pStop",
            "HERDR_SOCKET_PATH": str(root / "no-socket"),
            "PATH": str(root / "bin") + ":" + env.get("PATH", "/usr/bin:/bin"),
            "PYTHONPATH": str(shim),
            "STOP_AUDIT_PATH": str(audit),
            "STOP_HERDR_LOG": str(herdr_log),
            "STOP_TOOLS_DIR": str(WORKTREE / "tools"),
        })
        payload = json.dumps({"session_id": "stop-session", "hook_event_name": "Stop"})
        proc = subprocess.run(
            [sys.executable, "-B", str(WORKTREE / "hooks" / "herdr-session-projection.py")],
            input=payload, text=True, capture_output=True, env=env, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        events = [json.loads(line)["event"]
                  for line in audit.read_text(encoding="utf-8").splitlines() if line.strip()]
        self.assertEqual(events.count("may_report"), 1, events)
        self.assertEqual(events.count("read_markers"), 1, events)
        self.assertEqual(events.count("read_relations"), 1, events)
        self.assertNotIn("_projection_sessions", events)
        log = herdr_log.read_text(encoding="utf-8")
        self.assertIn("pane report-metadata", log)
        self.assertIn("--title", log)
        # The idle callback ran after the display report: it resolves the pane.
        self.assertIn("agent get w1:pStop", log)


if __name__ == "__main__":
    unittest.main(verbosity=2)
