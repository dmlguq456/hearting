#!/usr/bin/env python3
"""Claude host census: Hearting's own session-shell wrapper and ``--fork-session``.

The wrapper repeats its native Claude child's argv, so it stops blocking the
census only while that child is confirmed by its own native session record.
``--fork-session`` with ``--session-id`` runs the named id, not the resumed one.
Every case that could read a live session as ended stays incomplete or alive.
"""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])

import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest import mock

import claude_process_identity
from claude_process_identity import claude_session_census
from recipient_lifetime import RecipientCensus

HOST_NS = "pid:[4026531836]"
NATIVE = "/home/u/.local/bin/claude"
NATIVE_EXE = "/home/u/.local/share/claude/versions/2.1.323"
ENTRY = "/home/u/.local/share/hearting/releases/v9/utilities/session-shell/entry.py"
HOST = "/home/u/.local/share/hearting/releases/v9/utilities/session-shell/host.cjs"
OLD, LIVE, FORK = ("11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222",
                   "33333333-3333-4333-8333-333333333333")


class CensusFixture(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        base = Path(temp.name)
        self.root = base / "proc"
        (self.root / "self" / "ns").mkdir(parents=True)
        os.symlink(HOST_NS, self.root / "self" / "ns" / "pid")
        self.home = base / "user" / ".claude"
        (self.home / "sessions").mkdir(parents=True)
        (self.home / "projects" / "p").mkdir(parents=True)

    def process(self, pid, argv, *, exe, ppid=1, ns=HOST_NS, nspid=None):
        proc = self.root / str(pid)
        (proc / "ns").mkdir(parents=True)
        # Fields after "comm) ": state, ppid, 17 others, start ticks (field 22).
        (proc / "stat").write_text(f"{pid} (x) S {ppid} " + " ".join(["0"] * 17 + [str(pid)]))
        (proc / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")
        os.symlink(exe, proc / "exe")
        os.symlink(ns, proc / "ns" / "pid")
        if nspid is not None:
            (proc / "status").write_text(f"NSpid:\t{pid}\t{nspid}\n")
        (proc / "environ").write_bytes(f"HOME={self.home.parent}\0".encode())
        return proc

    def native_record(self, pid, sid, *, start=None):
        (self.home / "sessions" / f"{pid}.json").write_text(json.dumps(
            {"pid": int(pid), "procStart": str(start or pid), "sessionId": sid}))

    def history(self, *sids):
        (self.home / "history.jsonl").write_text(
            "".join(json.dumps({"sessionId": sid}) + "\n" for sid in sids))

    def claude(self, pid, args, *, ppid=1, **kwargs):
        return self.process(pid, [NATIVE, *args], exe=NATIVE_EXE, ppid=ppid, **kwargs)

    def shell(self, entry_pid, host_pid, args, *, host_args=None, entry_ns=HOST_NS):
        self.process(entry_pid, ["python3", ENTRY, "claude", NATIVE, *args],
                     exe="/usr/bin/python3.12", ns=entry_ns)
        self.process(host_pid, ["/usr/bin/node", HOST, "claude", NATIVE,
                                *(args if host_args is None else host_args)],
                     exe="/usr/bin/node", ppid=entry_pid, ns=entry_ns)

    def census(self):
        return claude_session_census(proc_root=self.root, budget_seconds=30.0)

    def state(self, sid):
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.home)}), \
                mock.patch.object(claude_process_identity, "claude_session_census",
                                  side_effect=lambda: self.census()):
            return RecipientCensus().state("claude", sid)


class SessionShellWrapperTest(CensusFixture):
    def test_wrapper_over_a_confirmed_claude_no_longer_blocks_ended_sessions(self):
        for child_pid in (90, 300):  # the child sorts before or after its wrapper
            with self.subTest(child_pid=child_pid):
                self.setUp()
                self.shell(100, 101, ["--resume", LIVE])
                self.claude(child_pid, ["--resume", LIVE], ppid=100)
                self.native_record(child_pid, LIVE)
                self.history(OLD, LIVE)
                complete, holders, reasons = self.census()
                self.assertTrue(complete, reasons)
                self.assertEqual(holders, {LIVE})
                self.assertEqual(holders.process_claims, ((LIVE, frozenset({LIVE})),))
                self.assertEqual(self.state(LIVE), "alive")
                self.assertEqual(self.state(OLD), "ended")

    def test_claude_that_has_not_written_its_native_record_keeps_the_census_incomplete(self):
        self.shell(100, 101, ["--resume", LIVE])
        self.claude(102, ["--resume", LIVE], ppid=100)
        self.history(OLD, LIVE)
        complete, holders, reasons = self.census()
        self.assertFalse(complete)
        self.assertIn("session-identity-unresolved", reasons)
        self.assertIn(LIVE, holders)
        self.assertEqual(self.state(LIVE), "alive")
        self.assertEqual(self.state(OLD), "unknown")

    def test_wrapper_before_its_child_execs_claude_stays_incomplete(self):
        self.shell(100, 101, ["--resume", LIVE])
        # pty.fork's child still runs entry.py's argv until it execs the native CLI.
        self.process(102, ["python3", ENTRY, "claude", NATIVE, "--resume", LIVE],
                     exe="/usr/bin/python3.12", ppid=100)
        self.history(OLD)
        complete, _holders, reasons = self.census()
        self.assertFalse(complete)
        self.assertIn("session-identity-unresolved", reasons)
        self.assertEqual(self.state(OLD), "unknown")

    def test_wrapper_whose_claude_died_stays_incomplete(self):
        self.shell(100, 101, ["--resume", LIVE])
        self.history(OLD, LIVE)
        complete, holders, reasons = self.census()
        self.assertFalse(complete)
        self.assertIn("session-identity-unresolved", reasons)
        self.assertIn(LIVE, holders)
        self.assertEqual(self.state(LIVE), "alive")
        self.assertEqual(self.state(OLD), "unknown")

    def test_a_confirmed_claude_that_is_not_the_wrappers_child_does_not_explain_it(self):
        self.shell(100, 101, ["--resume", LIVE])
        self.claude(102, ["--resume", LIVE], ppid=1)
        self.native_record(102, LIVE)
        complete, _holders, reasons = self.census()
        self.assertFalse(complete)
        self.assertIn("session-identity-unresolved", reasons)

    def test_child_with_other_arguments_does_not_explain_the_wrapper(self):
        self.shell(100, 101, ["--resume", OLD])
        self.claude(102, [], ppid=100)
        self.native_record(102, LIVE)
        self.history(OLD)
        complete, holders, reasons = self.census()
        self.assertFalse(complete)
        self.assertIn("session-identity-unresolved", reasons)
        self.assertIn(OLD, holders, "the wrapper's own argv claim is kept")
        self.assertEqual(self.state(OLD), "alive")

    def test_child_in_another_pid_namespace_does_not_explain_the_wrapper(self):
        self.shell(100, 101, ["--resume", LIVE])
        self.claude(102, ["--resume", LIVE], ppid=100, ns="pid:[4026539999]", nspid=7)
        self.native_record(7, LIVE, start=102)
        self.history(OLD)
        complete, holders, reasons = self.census()
        self.assertFalse(complete)
        self.assertIn("session-identity-unresolved", reasons)
        self.assertIn(LIVE, holders)
        self.assertEqual(self.state(OLD), "unknown")

    def test_display_host_needs_its_own_confirmed_entry_and_the_same_argv(self):
        cases = {
            "orphan host": dict(entry=False),
            "host with other argv": dict(host_args=["--resume", OLD]),
        }
        for name, case in cases.items():
            with self.subTest(name):
                self.setUp()
                if case.get("entry", True):
                    self.shell(100, 101, ["--resume", LIVE], host_args=case.get("host_args"))
                else:
                    self.process(101, ["/usr/bin/node", HOST, "claude", NATIVE, "--resume", LIVE],
                                 exe="/usr/bin/node", ppid=1)
                    self.process(100, ["/bin/sh"], exe="/bin/sh")
                self.claude(102, ["--resume", LIVE], ppid=100)
                self.native_record(102, LIVE)
                complete, _holders, reasons = self.census()
                self.assertFalse(complete)
                self.assertIn("session-identity-unresolved", reasons)

    def test_same_session_held_by_two_processes_stays_alive_when_one_wrapper_loses_its_claude(self):
        self.shell(100, 101, ["--resume", LIVE])
        self.claude(102, ["--resume", LIVE], ppid=100)
        self.native_record(102, LIVE)
        self.claude(200, ["--resume", LIVE])
        self.native_record(200, LIVE)
        self.history(OLD, LIVE)
        complete, holders, reasons = self.census()
        self.assertTrue(complete, reasons)
        self.assertEqual(holders, {LIVE})
        self.assertEqual(self.state(LIVE), "alive")

        shutil.rmtree(self.root / "102")  # the wrapper's Claude exits; the other holder stays
        complete, holders, reasons = self.census()
        self.assertFalse(complete)
        self.assertIn("session-identity-unresolved", reasons)
        self.assertIn(LIVE, holders)
        self.assertEqual(self.state(LIVE), "alive")
        self.assertEqual(self.state(OLD), "unknown")


class ForkSessionTest(CensusFixture):
    def test_fork_with_session_id_runs_only_the_named_session(self):
        for args in (["--resume", OLD, "--session-id", FORK, "--fork-session"],
                     ["--fork-session", "--resume=" + OLD, "--session-id=" + FORK]):
            with self.subTest(args=args):
                self.setUp()
                self.claude(100, args)
                self.native_record(100, FORK)
                self.history(OLD, FORK)
                complete, holders, reasons = self.census()
                self.assertTrue(complete, reasons)
                self.assertEqual(holders, {FORK})
                self.assertEqual(self.state(FORK), "alive")
                self.assertEqual(self.state(OLD), "ended")

    def test_session_id_with_resume_but_without_fork_stays_conflicting(self):
        self.claude(100, ["--resume", OLD, "--session-id", FORK])
        self.native_record(100, FORK)
        self.history(OLD, FORK)
        complete, holders, reasons = self.census()
        self.assertFalse(complete)
        self.assertIn("conflicting-session-arguments", reasons)
        self.assertEqual(holders, {OLD, FORK})
        self.assertEqual(self.state(OLD), "alive")

    def test_fork_without_a_named_id_keeps_the_resumed_session(self):
        self.claude(100, ["--resume", OLD, "--fork-session"])
        self.native_record(100, FORK)
        self.history(OLD, FORK)
        complete, holders, reasons = self.census()
        self.assertTrue(complete, reasons)
        self.assertEqual(holders, {OLD, FORK})
        self.assertEqual(self.state(OLD), "alive")

    def test_fork_before_its_native_record_keeps_the_census_incomplete(self):
        self.claude(100, ["--resume", OLD, "--session-id", FORK, "--fork-session"])
        self.history(OLD)
        complete, holders, reasons = self.census()
        self.assertFalse(complete)
        self.assertIn("session-identity-unresolved", reasons)
        self.assertEqual(holders, {FORK})
        self.assertEqual(self.state(OLD), "unknown")

    def test_fork_inside_the_session_shell_wrapper(self):
        args = ["--resume", OLD, "--session-id", FORK, "--fork-session"]
        self.shell(100, 101, args)
        self.claude(102, args, ppid=100)
        self.native_record(102, FORK)
        self.history(OLD, FORK)
        complete, holders, reasons = self.census()
        self.assertTrue(complete, reasons)
        self.assertEqual(holders, {FORK})
        self.assertEqual(self.state(OLD), "ended")


if __name__ == "__main__":
    unittest.main()
