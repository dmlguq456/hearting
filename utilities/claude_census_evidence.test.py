#!/usr/bin/env python3
"""Claude host census: session-shell wrapper, ``--fork-session`` and Claude Code's daemon.

The wrapper repeats its native Claude child's argv, so it stops blocking the
census only while that child is confirmed by its own native session record.
``--fork-session`` with ``--session-id`` runs the named id, not the resumed one.
Claude Code's background supervisor holds every session its jobs name; its
waiting spare and pty host stop blocking only on exact evidence.
Every case that could read a live session as ended stays incomplete or alive.
"""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])

import json
from pathlib import Path
import shutil
import stat
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


BG, BG2 = ("44444444-4444-4444-8444-444444444444", "55555555-5555-4555-8555-555555555555")
SPARE_TITLE, PTY_TITLE = "claude bg-spare", "claude bg-pty-host"


class BackgroundSupervisorTest(CensusFixture):
    """Claude Code's ``daemon run`` supervisor, its ``--bg-pty-host`` and ``--bg-spare``."""

    def setUp(self):
        super().setUp()
        self.sockets = self.home.parent / "cc-daemon" / "spare"
        self.sockets.mkdir(parents=True)

    def supervisor(self, pid=10, *, lock_pid=None, lock_start=None):
        self.claude(pid, ["daemon", "run", "--json-path", str(self.home / "daemon.json")])
        if lock_pid is not False:
            (self.home / "daemon.lock").write_text(json.dumps(
                {"pid": lock_pid or pid, "procStart": str(lock_start or pid), "version": "2.1.323"}))
        # 2.1.323's layout: a roster naming the supervisor, and jobs/ holding one
        # directory per job beside pins.json.
        self.roster({}, supervisor=pid)
        (self.home / "jobs").mkdir(exist_ok=True)
        (self.home / "jobs" / "pins.json").write_text("{}")

    def roster(self, workers, *, supervisor=10):
        (self.home / "daemon").mkdir(exist_ok=True)
        (self.home / "daemon" / "roster.json").write_text(json.dumps(
            {"proto": 1, "supervisorPid": supervisor,
             "workers": {short: {"pid": 99, "sessionId": sid} for short, sid in workers.items()}}))

    def pinned_state(self, result, sid):
        """What a recipient reads from this one census result."""
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.home)}), \
                mock.patch.object(claude_process_identity, "claude_session_census", return_value=result):
            return RecipientCensus().state("claude", sid)

    def job(self, short, sid, state="working"):
        (self.home / "jobs" / short).mkdir(parents=True)
        (self.home / "jobs" / short / "state.json").write_text(
            json.dumps({"state": state, "sessionId": sid}))

    def spare(self, host_pid, spare_pid, *, parent=10, waiting=True, short="aaaa1111"):
        claim = str(self.sockets / f"{short}.claim.sock")
        self.process(host_pid, [PTY_TITLE, "--bg-pty-host", str(self.sockets / f"{short}.pty.sock"),
                                "200", "50", "--", NATIVE_EXE, "--bg-spare", claim],
                     exe=NATIVE_EXE, ppid=parent)
        proc = self.process(spare_pid, [SPARE_TITLE, "--bg-spare", claim], exe=NATIVE_EXE, ppid=host_pid)
        if waiting:
            os.mknod(claim, stat.S_IFSOCK | 0o600)
            os.symlink(str(self.sockets), proc / "cwd")
        else:  # taking a session: the claim socket is gone and it moved to the work tree
            os.symlink(str(self.home.parent), proc / "cwd")
        return claim

    def idle_supervisor(self):
        self.supervisor()
        self.spare(20, 21)
        self.job("bbbb2222", BG, state="stopped")
        self.history(OLD, BG)

    def test_idle_supervisor_and_waiting_spare_no_longer_block_ended_sessions(self):
        self.idle_supervisor()
        complete, holders, reasons = self.census()
        self.assertTrue(complete, reasons)
        self.assertEqual(holders, {BG}, "every job the supervisor may carry stays held")
        self.assertEqual(holders.process_claims, ((None, frozenset({BG})),))
        self.assertEqual(self.state(OLD), "ended")
        self.assertEqual(self.state(BG), "alive")

    def test_session_between_workers_stays_alive_through_the_job_record(self):
        # A respawn stops the old worker, its job reads "stopped" and no process or
        # roster entry names the session until a spare takes it about 0.1 s later.
        self.idle_supervisor()
        self.job("cccc3333", BG2, state="stopped")
        self.history(OLD, BG, BG2)
        complete, holders, reasons = self.census()
        self.assertTrue(complete, reasons)
        self.assertEqual(holders, {BG, BG2})
        self.assertEqual(self.state(BG2), "alive")
        self.assertEqual(self.state(OLD), "ended")

    def test_supervisor_restart_and_unproven_supervisor_stay_incomplete(self):
        cases = {
            "lock not written yet": dict(lock_pid=False),
            "lock names the next supervisor": dict(lock_pid=11),
            "lock names an earlier process with this pid": dict(lock_start=999),
        }
        for name, case in cases.items():
            with self.subTest(name):
                self.setUp()
                self.supervisor(**case)
                self.spare(20, 21)
                self.history(OLD)
                complete, _holders, reasons = self.census()
                self.assertFalse(complete)
                self.assertIn("session-identity-unresolved", reasons)
                self.assertEqual(self.state(OLD), "unknown")

    def test_unreadable_job_record_keeps_the_supervisor_unresolved(self):
        def empty_job():
            (self.home / "jobs" / "dddd4444").mkdir(parents=True)

        def malformed_job():
            empty_job()
            (self.home / "jobs" / "dddd4444" / "state.json").write_text("{")

        def linked_job():
            (self.home / "jobs").mkdir(parents=True, exist_ok=True)
            os.symlink(str(self.home), self.home / "jobs" / "dddd4444")

        for make in (empty_job, malformed_job, linked_job):
            with self.subTest(make.__name__):
                self.setUp()
                self.idle_supervisor()
                make()
                complete, _holders, reasons = self.census()
                self.assertFalse(complete)
                self.assertIn("session-identity-unresolved", reasons)
                self.assertEqual(self.state(OLD), "unknown")

    def test_spare_that_took_a_session_stays_incomplete_until_its_native_record(self):
        self.supervisor()
        self.spare(20, 21, waiting=False)
        self.job("bbbb2222", BG)
        self.history(OLD, BG)
        complete, holders, reasons = self.census()
        self.assertFalse(complete)
        self.assertIn("session-identity-unresolved", reasons)
        self.assertEqual(self.state(BG), "alive")
        self.assertEqual(self.state(OLD), "unknown")

        self.native_record(21, BG)  # the spare now runs the session itself
        complete, holders, reasons = self.census()
        self.assertTrue(complete, reasons)
        self.assertEqual(holders, {BG})
        self.assertEqual(self.state(BG), "alive")
        self.assertEqual(self.state(OLD), "ended")

    def test_spare_without_its_exact_host_and_supervisor_stays_incomplete(self):
        def orphan_host():  # the supervisor stopped; its pty host now belongs to init
            self.supervisor()
            self.spare(20, 21, parent=1)

        def no_supervisor():
            self.process(10, ["/bin/sh"], exe="/bin/sh")
            self.spare(20, 21)

        def other_claim_socket():
            self.supervisor()
            claim = self.spare(20, 21)
            other = self.sockets / "eeee5555.claim.sock"
            os.mknod(other, stat.S_IFSOCK | 0o600)
            (self.root / "21" / "cmdline").write_bytes(
                b"\0".join(a.encode() for a in [SPARE_TITLE, "--bg-spare", str(other)]) + b"\0")
            self.assertNotEqual(claim, str(other))

        def socket_not_bound_yet():
            self.supervisor()
            claim = self.spare(20, 21)
            os.unlink(claim)

        def outside_its_spare_directory():
            self.supervisor()
            self.spare(20, 21)
            os.unlink(self.root / "21" / "cwd")
            os.symlink(str(self.home.parent), self.root / "21" / "cwd")

        for make in (orphan_host, no_supervisor, other_claim_socket, socket_not_bound_yet,
                     outside_its_spare_directory):
            with self.subTest(make.__name__):
                self.setUp()
                make()
                self.history(OLD)
                complete, _holders, reasons = self.census()
                self.assertFalse(complete)
                self.assertIn("session-identity-unresolved", reasons)
                self.assertEqual(self.state(OLD), "unknown")

    def test_spare_taking_a_session_during_the_census_is_caught_when_it_closes(self):
        self.idle_supervisor()
        claim = str(self.sockets / "aaaa1111.claim.sock")
        real = claude_process_identity._runtime_identity
        calls = []

        def identity(proc):
            if Path(proc).name == "21":
                calls.append(proc)
                if len(calls) == 2 and os.path.exists(claim):  # the closing inventory
                    os.unlink(claim)
            return real(proc)

        with mock.patch.object(claude_process_identity, "_runtime_identity", side_effect=identity):
            complete, holders, reasons = self.census()
        self.assertFalse(complete)
        self.assertEqual(reasons, {"process-inventory-changed"})
        self.assertIn(BG, holders)

    def test_spare_taking_a_new_job_right_after_the_closing_check_read_its_directory(self):
        # Review 1610: a job written after the jobs were read goes to the spare, and
        # connect -> unlink -> chdir happens right after the closing check read the
        # spare's directory, before its native record.
        self.idle_supervisor()
        self.history(OLD, BG, BG2)
        claim = str(self.sockets / "aaaa1111.claim.sock")
        cwd = self.root / "21" / "cwd"
        real_identity, real_readlink = claude_process_identity._runtime_identity, os.readlink
        identities, cwd_reads = [], []

        def identity(proc):
            if Path(proc).name == "21":
                identities.append(proc)
                if len(identities) == 2:  # the closing inventory
                    self.job("eeee6666", BG2)
            return real_identity(proc)

        def readlink(path, *args, **kwargs):
            value = real_readlink(path, *args, **kwargs)
            if os.fspath(path) == str(cwd):
                cwd_reads.append(value)
                if len(cwd_reads) == 2:  # the closing check
                    os.unlink(claim)
                    os.unlink(cwd)
                    os.symlink(str(self.home.parent), cwd)
            return value

        with mock.patch.object(claude_process_identity, "_runtime_identity", side_effect=identity), \
                mock.patch.object(claude_process_identity.os, "readlink", side_effect=readlink):
            result = self.census()
        complete, holders, reasons = result
        self.assertEqual(len(cwd_reads), 2)
        self.assertFalse(complete, holders)
        self.assertEqual(reasons, {"process-inventory-changed"})
        self.assertNotEqual(self.pinned_state(result, BG2), "ended")

    def test_session_handed_to_a_spare_after_the_jobs_were_read_keeps_the_census_incomplete(self):
        # The supervisor connects to the claim socket 1-30 ms before the spare removes
        # it, so the spare still looks waiting; the new job is caught by reading the
        # jobs again after the spare's closing check.
        self.idle_supervisor()
        self.history(OLD, BG, BG2)
        real = claude_process_identity._runtime_identity
        calls = []

        def identity(proc):
            if Path(proc).name == "21":
                calls.append(proc)
                if len(calls) == 2:  # the closing inventory
                    self.job("eeee6666", BG2)
            return real(proc)

        with mock.patch.object(claude_process_identity, "_runtime_identity", side_effect=identity):
            result = self.census()
        complete, holders, reasons = result
        self.assertFalse(complete, holders)
        self.assertEqual(reasons, {"process-inventory-changed"})
        self.assertNotEqual(self.pinned_state(result, BG2), "ended")

    def test_jobs_in_an_unknown_layout_keep_the_supervisor_unresolved(self):
        # Review 1610: a lock naming the supervisor does not prove how this version
        # stores its jobs. Only 2.1.323's own layout counts as a known empty state.
        def no_jobs_directory():
            shutil.rmtree(self.home / "jobs")

        def flat_job_file():
            (self.home / "jobs" / "job.json").write_text(json.dumps({"sessionId": BG2}))

        def job_directory_named_otherwise():
            self.job("job-0001", BG2)

        def no_roster():
            (self.home / "daemon" / "roster.json").unlink()

        def roster_of_another_supervisor():
            self.roster({}, supervisor=11)

        def roster_worker_without_its_job():
            self.roster({"eeee6666": BG2})

        cases = [(no_jobs_directory, {"eeee6666": BG2}), (flat_job_file, {"eeee6666": BG2}),
                 (no_jobs_directory, {}), (flat_job_file, {}), (job_directory_named_otherwise, {}),
                 (no_roster, {}), (roster_of_another_supervisor, {}),
                 (roster_worker_without_its_job, {})]
        for make, workers in cases:
            with self.subTest(make.__name__, roster=workers):
                self.setUp()
                self.supervisor()
                self.spare(20, 21)
                self.roster(workers)
                self.history(OLD, BG2)
                make()
                complete, _holders, reasons = self.census()
                self.assertFalse(complete)
                self.assertIn("session-identity-unresolved", reasons)
                self.assertNotEqual(self.state(BG2), "ended")
                self.assertEqual(self.state(OLD), "unknown")

    def test_known_layout_with_pins_and_a_roster_worker_is_complete(self):
        self.supervisor()
        self.spare(20, 21)
        self.job("bbbb2222", BG)
        self.roster({"bbbb2222": BG})
        self.history(OLD, BG)
        complete, holders, reasons = self.census()
        self.assertTrue(complete, reasons)
        self.assertEqual(holders, {BG})
        self.assertEqual(self.state(BG), "alive")
        self.assertEqual(self.state(OLD), "ended")

    def test_old_pty_host_over_a_confirmed_session_is_explained_by_its_child(self):
        tail = [NATIVE_EXE, "--session-id", LIVE, "--fork-session", "--resume",
                str(self.home / "projects" / "p" / (OLD + ".jsonl")), "--permission-mode", "default"]
        self.process(30, [PTY_TITLE, "--bg-pty-host", "/tmp/cc-daemon-1/x/pty/b8.sock", "200", "50",
                          "--", *tail], exe=NATIVE_EXE + " (deleted)", ppid=1)
        self.process(31, tail, exe=NATIVE_EXE + " (deleted)", ppid=30)
        self.native_record(31, LIVE)
        self.history(OLD, LIVE)
        complete, holders, reasons = self.census()
        self.assertTrue(complete, reasons)
        self.assertEqual(holders, {LIVE})
        self.assertEqual(self.state(LIVE), "alive")
        self.assertEqual(self.state(OLD), "ended")

    def test_old_pty_host_without_a_confirmed_child_stays_incomplete_and_alive(self):
        tail = [NATIVE_EXE, "--session-id", LIVE, "--fork-session", "--resume", OLD]
        cases = {"no child": False, "child without its native record": True,
                 "child running other arguments": "other"}
        for name, child in cases.items():
            with self.subTest(name):
                self.setUp()
                self.process(30, [PTY_TITLE, "--bg-pty-host", "/tmp/s.sock", "200", "50", "--", *tail],
                             exe=NATIVE_EXE, ppid=1)
                if child:
                    self.process(31, tail if child is True else [NATIVE_EXE], exe=NATIVE_EXE, ppid=30)
                if child == "other":
                    self.native_record(31, BG)
                self.history(OLD, LIVE)
                complete, holders, reasons = self.census()
                self.assertFalse(complete)
                self.assertIn("session-identity-unresolved", reasons)
                self.assertIn(LIVE, holders)
                self.assertEqual(self.state(LIVE), "alive")
                self.assertEqual(self.state(OLD), "unknown")

    def test_same_session_in_a_job_and_two_processes_stays_alive_when_one_worker_goes(self):
        self.supervisor()
        self.spare(20, 21, waiting=False)
        self.native_record(21, BG)
        self.job("bbbb2222", BG)
        self.claude(40, ["--resume", BG])
        self.native_record(40, BG)
        self.history(OLD, BG)
        complete, holders, reasons = self.census()
        self.assertTrue(complete, reasons)
        self.assertEqual(holders, {BG})
        self.assertEqual(self.state(BG), "alive")

        shutil.rmtree(self.root / "21")  # the worker exits; its pty host is left alone
        complete, holders, reasons = self.census()
        self.assertFalse(complete)
        self.assertIn("session-identity-unresolved", reasons)
        self.assertIn(BG, holders)
        self.assertEqual(self.state(BG), "alive")
        self.assertEqual(self.state(OLD), "unknown")


if __name__ == "__main__":
    unittest.main()
