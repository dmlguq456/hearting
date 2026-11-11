#!/usr/bin/env python3
"""A sealed ordinary peer message keeps its courier, its window namespace and its notice.

Synthetic I/O only: the sealed row, the courier pass and the session table are real
peer-steward code; native delivery, the shell host and the runner launch are stubbed.
"""
import importlib.util
import io
import contextlib
import os
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

FIXTURE = sys.modules.get("peer_steward_test_fixture")
if FIXTURE is None:
    _FIXTURE_SPEC = importlib.util.spec_from_file_location(
        "peer_steward_test_fixture", Path(__file__).resolve().with_name("peer_steward.test.py"))
    FIXTURE = importlib.util.module_from_spec(_FIXTURE_SPEC)
    sys.modules[_FIXTURE_SPEC.name] = FIXTURE
    _FIXTURE_SPEC.loader.exec_module(FIXTURE)
steward = FIXTURE.peer_steward
import peer_address  # noqa: E402

SENDER = {"harness": "codex", "session_id": "00000000-0000-4000-8000-000000000090",
          "name": "fixture sender"}
RECIPIENT_SID = "00000000-0000-4000-8000-000000000091"


class SealedMessageDutyTest(FIXTURE._TmpRootMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        runner = mock.patch.object(steward.peer_obligations, "ensure_normal_runner",
                                   return_value=False)
        runner.start()
        self.addCleanup(runner.stop)
        self.store = steward.peer_obligations.ObligationStore()

    def prompt(self, *, fail_reads):
        """``_cmd_prompt_native`` with the first ``fail_reads`` reads after sealing failing."""
        pm = steward.peer_message
        original, refs, failed = pm._read_pending, [], []
        body = "sealed body " + str(fail_reads)
        args = steward.build_parser().parse_args(["prompt", "claude:" + RECIPIENT_SID, body])

        def read(ref, *a, **kw):
            if refs and ref == refs[-1] and len(failed) < fail_reads:
                failed.append(ref)
                raise OSError("fixture read failure after seal")
            return original(ref, *a, **kw)

        out = io.StringIO()
        with mock.patch.object(pm, "_read_pending", side_effect=read), \
                contextlib.redirect_stdout(out):
            code = steward._cmd_prompt_native(
                args, body, "claude", RECIPIENT_SID,
                from_identity=(SENDER["session_id"], "codex", "fixture"),
                from_name=SENDER["name"], on_prepared=refs.append)
        self.assertEqual(len(refs), 1)
        return code, out.getvalue(), refs[0], original(refs[0])

    def test_a_read_failure_after_sealing_keeps_the_courier_and_prints_the_ref(self):
        for fail_reads in (0, 1, 3):
            with self.subTest(fail_reads=fail_reads):
                code, out, ref, row = self.prompt(fail_reads=fail_reads)
                self.assertEqual(row["state"], "pending")
                duty = self.store.get(steward._message_obligation_id(ref))
                self.assertIsNotNone(duty, out)
                self.assertIn("ref=" + ref, out)
                # The courier keeps the sealed recipient, never a re-resolved name.
                self.assertEqual(duty["intent"]["to"]["session_id"], RECIPIENT_SID)
                self.assertEqual(duty["intent"]["body_digest"], row["body_sha256"])
                if fail_reads:
                    self.assertEqual(code, 5)
                    self.assertIn("prompted=unverified reason=peer-pending-unavailable", out)

    def overdue_duty(self, label, target="w1:p1"):
        pm = steward.peer_message
        recipient = {"harness": "claude", "session_id": RECIPIENT_SID}
        _text, ref = pm.prepare_peer_message("overdue " + label, SENDER, recipient, defer=True,
                                             original_target=target, server="default")
        row = pm._read_pending(ref)
        pm._save_pending(dict(row, created=time.time() - steward._FLUSH_STUCK_HOURS * 3600 - 30))
        duty = steward._schedule_message_obligation(pm._read_pending(ref), target, target)
        self.assertIsNotNone(duty)
        return ref, duty

    def test_an_identity_refusal_still_owes_the_sender_one_delay_notice(self):
        for reason in ("attachment-missing", "target-identity-changed"):
            with self.subTest(reason=reason):
                ref, duty = self.overdue_duty(reason)
                with mock.patch.object(steward, "_current_shell_binding",
                                       return_value=(None, reason)):
                    for _ in range(2):
                        steward._resume_message_obligation(self.store.get(duty["id"]), self.store)
                # Delivery stays refused and the sealed body stays pending.
                self.assertEqual(steward.peer_message._read_pending(ref)["state"], "pending")
                notice = self.store.get("delay-" + ref)
                self.assertIsNotNone(notice)
                self.assertEqual(notice["intent"]["to"]["session_id"], SENDER["session_id"])

    def test_an_unreadable_sealed_row_still_dates_the_notice_from_its_duty(self):
        ref, duty = self.overdue_duty("unreadable")
        accepted = time.time() - steward._FLUSH_STUCK_HOURS * 3600 - 30
        record = dict(self.store.get(duty["id"]), accepted_at=accepted)
        with mock.patch.object(steward.peer_message, "_read_pending",
                               side_effect=OSError("fixture unreadable")):
            steward._resume_message_obligation(record, self.store)
        self.assertEqual(self.store.get(duty["id"])["observation"]["reason"],
                         "pending-row-unreadable")
        self.assertIsNotNone(self.store.get("delay-" + ref))

    def test_a_retained_window_address_is_reread_only_on_its_first_namespace(self):
        # The sender's environment names server "hb" under configuration A, no socket value.
        a, b = self.tmp_root / "a", self.tmp_root / "b"
        sock_a = str(a / "herdr" / "sessions" / "hb" / "herdr.sock")
        sock_b = str(b / "herdr" / "sessions" / "hb" / "herdr.sock")
        native_b = {"harness": "claude", "sid": RECIPIENT_SID, "pid": 0, "binding": None,
                    "env": {"HERDR_PANE_ID": "w1:p1", "HERDR_SESSION": "hb",
                            "HERDR_SOCKET_PATH": sock_b, "XDG_CONFIG_HOME": str(b)}}
        os.environ.update(AGENT_HERDR_SESSION="hb", XDG_CONFIG_HOME=str(a))
        old_server = steward._HERDR_SESSION
        steward._HERDR_SESSION = "hb"
        self.addCleanup(setattr, steward, "_HERDR_SESSION", old_server)
        args = steward.build_parser().parse_args(["prompt", "w1:p1", "only for A"])
        out = io.StringIO()
        with mock.patch.object(peer_address, "native_sessions", return_value=[native_b]), \
                mock.patch.object(steward, "_current_session_identity",
                                  return_value=(SENDER["session_id"], "codex")), \
                contextlib.redirect_stdout(out):
            code = steward.cmd_prompt(args)
        self.assertEqual(code, 5, out.getvalue())
        self.assertIn("reason=target-pane-unresolved", out.getvalue())
        duty = next(row for row in self.store.list(kinds={"message"})
                    if row["intent"].get("target") == "w1:p1")
        self.assertEqual(duty["intent"]["identity"]["namespace"], sock_a)
        # The courier runs in B's environment; the retained address still means A's window.
        os.environ["XDG_CONFIG_HOME"] = str(b)
        delivered = []
        with mock.patch.object(peer_address, "native_sessions", return_value=[native_b]), \
                mock.patch.object(steward.peer_message, "bind_unresolved_pending_recipient",
                                  side_effect=lambda *a: delivered.append(a)):
            steward._resume_message_obligation(self.store.get(duty["id"]), self.store)
        self.assertEqual(delivered, [])
        self.assertEqual(self.store.get(duty["id"])["observation"]["reason"],
                         "target-pane-unresolved")


class InboxRecipientsTest(FIXTURE._TmpRootMixin, unittest.TestCase):
    def test_inbox_upkeep_finds_live_sessions_from_hearting_records_without_herdr(self):
        import peer_steward_duties as duties
        live = [{"harness": "claude", "sid": "sid-live", "pid": 0, "env": {}, "binding": None},
                {"harness": "codex", "sid": "", "pid": 0, "env": {}, "binding": None}]
        with mock.patch.object(steward.herdr_observation, "pane_census",
                               side_effect=AssertionError("no Herdr census")) as census, \
                mock.patch.object(steward.subprocess, "run",
                                  side_effect=AssertionError("no subprocess")), \
                mock.patch.object(peer_address, "native_sessions", return_value=live):
            found = duties._live_recipients(self.tmp_root)
            steward._prefill_live_inboxes(self.tmp_root,
                                          deadline_ns=time.monotonic_ns() + 1_000_000_000)
        self.assertEqual(census.call_count, 0)
        self.assertIn(("claude", "sid-live"), found)
        self.assertNotIn(("codex", ""), found)


if __name__ == "__main__":
    unittest.main()
