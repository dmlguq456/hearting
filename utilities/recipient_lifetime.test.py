#!/usr/bin/env python3

import contextlib
import unittest
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
from unittest import mock

from recipient_lifetime import RecipientCensus, recipient_state
import claude_process_identity
from claude_process_identity import claude_session_census, invocation_kind


class RecipientLifetimeTest(unittest.TestCase):
    def _fake_runtime(self, root, number, *, exe="/claude/versions/2.1.30x",
                      argv=("claude",), uid=None, home=None):
        proc = root / str(number)
        (proc / "ns").mkdir(parents=True)
        (proc / "stat").write_text(f"{number} (claude) S " + " ".join(["0"] * 18 + [str(number)]))
        (proc / "cmdline").write_bytes(b"\0".join(x.encode() for x in argv) + b"\0")
        os.symlink("pid:[4026531836]", proc / "ns" / "pid")
        os.symlink(exe, proc / "exe")
        if home is None:
            home = root.parent / f"home-{number}" / ".claude"
        (home / "sessions").mkdir(parents=True)
        (proc / "environ").write_bytes(f"HOME={home.parent}\0".encode())
        return proc, home

    def _fake_proc_root(self, root):
        (root / "self" / "ns").mkdir(parents=True)
        os.symlink("pid:[4026531836]", root / "self" / "ns" / "pid")

    def _recipient_fixture(self, base):
        root = base / "proc"
        self._fake_proc_root(root)
        home = base / "native-home" / ".claude"
        (home / "projects" / "project-a").mkdir(parents=True)
        return root, home

    def _real_census_at(self, root):
        return mock.patch.object(
            claude_process_identity, "claude_session_census",
            side_effect=lambda: claude_session_census(proc_root=root))

    def test_fake_proc_non_initial_observer_namespace_is_incomplete_without_processes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "proc"
            self._fake_proc_root(root)
            (root / "self" / "ns" / "pid").unlink()
            os.symlink("pid:[4026532999]", root / "self" / "ns" / "pid")

            complete, holders, reasons = claude_session_census(proc_root=root)

            self.assertFalse(complete)
            self.assertEqual(holders, set())
            self.assertEqual(reasons, {"observer-not-in-initial-pid-namespace"})

    def test_exact_positive_evidence_and_successor_precedence(self):
        self.assertEqual(recipient_state(
            harness="claude", session_id="sid", seat_state="ended",
            successor_state="alive"), "alive")
        self.assertEqual(recipient_state(
            harness="codex", session_id="sid", lifetime_state="ended"), "ended")
        self.assertEqual(recipient_state(
            harness="opencode", session_id="sid", lifetime_state="ended"), "ended")

    def test_seatless_codex_and_opencode_without_existing_lifetime_stay_unknown(self):
        for harness in ("codex", "opencode"):
            with self.subTest(harness=harness):
                self.assertEqual(recipient_state(harness=harness, session_id="sid"), "unknown")

    def test_claude_census_is_pass_local_and_exceptions_are_unknown(self):
        calls = []
        census = RecipientCensus(lambda harness, sid: calls.append((harness, sid)) or "ended")
        self.assertEqual(recipient_state(harness="claude", session_id="sid", census=census), "ended")
        self.assertEqual(recipient_state(harness="claude", session_id="sid", census=census), "ended")
        self.assertEqual(calls, [("claude", "sid")])
        broken = RecipientCensus(lambda *_args: (_ for _ in ()).throw(OSError("unreadable")))
        self.assertEqual(recipient_state(harness="claude", session_id="sid", census=broken), "unknown")

    def test_unknown_probe_values_never_authorize_end(self):
        census = RecipientCensus(lambda *_args: "complete-but-unrecognized")
        self.assertEqual(recipient_state(harness="claude", session_id="sid", census=census), "unknown")
        self.assertEqual(recipient_state(harness="claude", session_id="sid",
                                         successor_state="conflict", census=census), "unknown")

    def test_initial_namespace_complete_empty_census_requires_prior_existence(self):
        with tempfile.TemporaryDirectory() as temp:
            root, home = self._recipient_fixture(Path(temp))
            with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(home)}), self._real_census_at(root):
                complete, holders, reasons = claude_session_census(proc_root=root)
                self.assertTrue(complete, reasons)
                self.assertEqual(holders, set())
                self.assertEqual(reasons, set())

                census = RecipientCensus()
                self.assertEqual(census.state("claude", "unseen-session"), "unknown")
                (home / "history.jsonl").write_text(
                    json.dumps({"sessionId": "unseen-session"}) + "\n", encoding="utf-8")
                self.assertEqual(census.state("claude", "unseen-session"), "unknown",
                                 "evidence is memoized within one recipient pass")
                self.assertEqual(RecipientCensus().state("claude", "unseen-session"), "ended")

    def test_exact_native_transcript_proves_existence_with_complete_empty_census(self):
        with tempfile.TemporaryDirectory() as temp:
            root, home = self._recipient_fixture(Path(temp))
            (home / "projects" / "project-a" / "transcript-session.jsonl").write_text(
                "{}\n", encoding="utf-8")
            with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(home)}), self._real_census_at(root):
                self.assertEqual(RecipientCensus().state("claude", "transcript-session"), "ended")

    def test_native_history_only_proves_exact_session_existence(self):
        with tempfile.TemporaryDirectory() as temp:
            root, home = self._recipient_fixture(Path(temp))
            (home / "history.jsonl").write_text(
                json.dumps({"sessionId": "history-only-session"}) + "\n", encoding="utf-8")
            with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(home)}), self._real_census_at(root):
                self.assertEqual(RecipientCensus().state("claude", "history-only-session"), "ended")
                self.assertFalse(any((home / "projects").glob("*/history-only-session.jsonl")))

    def test_live_holder_and_incomplete_census_keep_existing_priority(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root, home = self._recipient_fixture(base)
            proc, _runtime_home = self._fake_runtime(
                root, 123, argv=("claude", "--resume", "live-without-transcript"), home=home)
            (home / "sessions" / "123.json").write_text(json.dumps({
                "pid": 123, "procStart": "123", "sessionId": "live-without-transcript"}),
                encoding="utf-8")
            with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(home)}), self._real_census_at(root):
                complete, holders, reasons = claude_session_census(proc_root=root)
                self.assertTrue(complete, reasons)
                self.assertEqual(holders, {"live-without-transcript"})
                self.assertEqual(RecipientCensus().state("claude", "live-without-transcript"), "alive")

            incomplete_root = base / "incomplete-proc"
            self._fake_proc_root(incomplete_root)
            (incomplete_root / "self" / "ns" / "pid").unlink()
            os.symlink("pid:[4026532999]", incomplete_root / "self" / "ns" / "pid")
            (home / "history.jsonl").write_text(
                json.dumps({"sessionId": "recorded-but-incomplete"}) + "\n", encoding="utf-8")
            with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(home)}), self._real_census_at(incomplete_root):
                complete, holders, reasons = claude_session_census(proc_root=incomplete_root)
                self.assertFalse(complete)
                self.assertEqual(holders, set())
                self.assertEqual(reasons, {"observer-not-in-initial-pid-namespace"})
                self.assertEqual(RecipientCensus().state("claude", "recorded-but-incomplete"), "unknown")

    def test_history_requires_a_complete_exact_session_id_row(self):
        with tempfile.TemporaryDirectory() as temp:
            root, home = self._recipient_fixture(Path(temp))
            (home / "history.jsonl").write_text(
                json.dumps({"sessionId": "another-session"}) + "\n"
                + json.dumps({"message": "wanted-session"}) + "\n"
                + json.dumps({"sessionId": 7}) + "\n"
                + "{malformed json}\n"
                + json.dumps({"sessionId": "wanted-session"}),
                encoding="utf-8")
            with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(home)}), self._real_census_at(root):
                self.assertEqual(RecipientCensus().state("claude", "wanted-session"), "unknown")
                for unsafe in ("../wanted-session", "wanted*session"):
                    with self.subTest(unsafe=unsafe):
                        self.assertEqual(RecipientCensus().state("claude", unsafe), "unknown")

    def test_untrusted_native_evidence_never_proves_existence(self):
        for kind in ("symlink", "transcript-symlink", "remote", "foreign-owner", "replaced"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as temp:
                root, home = self._recipient_fixture(Path(temp))
                history = home / "history.jsonl"
                evidence = json.dumps({"sessionId": "guarded-session"}) + "\n"
                if kind == "symlink":
                    target = Path(temp) / "outside-history.jsonl"
                    target.write_text(evidence, encoding="utf-8")
                    history.symlink_to(target)
                elif kind == "transcript-symlink":
                    target = Path(temp) / "outside-transcript.jsonl"
                    target.write_text("{}\n", encoding="utf-8")
                    (home / "projects" / "project-a" / "guarded-session.jsonl").symlink_to(target)
                else:
                    history.write_text(evidence, encoding="utf-8")
                guards = [
                    mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(home)}),
                    mock.patch.object(claude_process_identity, "claude_session_census",
                                      return_value=(True, set(), set())),
                ]
                if kind == "remote":
                    guards.append(mock.patch(
                        "recipient_lifetime.is_local_path", return_value=False))
                if kind == "foreign-owner":
                    current_uid = os.getuid()
                    guards.append(mock.patch("recipient_lifetime.os.getuid",
                                             return_value=current_uid + 1))
                if kind == "replaced":
                    real_lstat = Path.lstat
                    reads = [0]

                    def changing_lstat(path, *args, **kwargs):
                        info = real_lstat(path, *args, **kwargs)
                        if Path(path) == history:
                            reads[0] += 1
                            if reads[0] > 1:
                                return SimpleNamespace(
                                    st_dev=info.st_dev, st_ino=info.st_ino, st_mode=info.st_mode,
                                    st_uid=info.st_uid, st_size=info.st_size,
                                    st_mtime_ns=info.st_mtime_ns + 1,
                                    st_ctime_ns=info.st_ctime_ns)
                        return info

                    guards.append(mock.patch.object(Path, "lstat", changing_lstat))
                with contextlib.ExitStack() as stack:
                    for guard in guards:
                        stack.enter_context(guard)
                    self.assertEqual(RecipientCensus().state("claude", "guarded-session"), "unknown")

    def test_fake_proc_exact_current_session_and_unresolved_runtime(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "proc"
            home = Path(temp) / "home" / ".claude"
            process = root / "123"
            (process / "ns").mkdir(parents=True)
            (root / "self" / "ns").mkdir(parents=True)
            (home / "sessions").mkdir(parents=True)
            (home / ".statusline").mkdir(parents=True)
            os.symlink("pid:[4026531836]", root / "self" / "ns" / "pid")
            os.symlink("pid:[4026531836]", process / "ns" / "pid")
            os.symlink("/usr/bin/claude", process / "exe")
            (process / "stat").write_text("123 (claude) S " + " ".join(["0"] * 18 + ["42"]))
            (process / "cmdline").write_bytes(b"claude\0")
            (process / "environ").write_bytes(f"HOME={home.parent}\0".encode())
            (home / "sessions" / "123.json").write_text(json.dumps({
                "pid": 123, "procStart": "42", "sessionId": "current-sid"}))

            complete, holders, reasons = claude_session_census(proc_root=root)
            self.assertTrue(complete, reasons)
            self.assertEqual(holders, {"current-sid"})

            (home / "sessions" / "123.json").unlink()
            complete, holders, reasons = claude_session_census(proc_root=root)
            self.assertFalse(complete)
            self.assertIn("session-identity-unresolved", reasons)
            (process / "cmdline").write_bytes(b"claude\0daemon\0")
            complete, holders, reasons = claude_session_census(proc_root=root)
            self.assertFalse(complete)
            self.assertEqual(holders, set())
            self.assertIn("session-identity-unresolved", reasons)

            (home / "sessions" / "123.json").write_text(json.dumps({
                "pid": 123, "procStart": "42", "sessionId": "daemon-sid"}))
            complete, holders, reasons = claude_session_census(proc_root=root)
            self.assertTrue(complete, reasons)
            self.assertEqual(holders, {"daemon-sid"})

            (home / "sessions" / "123.json").unlink()
            (process / "cmdline").write_bytes(b"claude\0remote-control\0")
            complete, holders, reasons = claude_session_census(proc_root=root)
            self.assertFalse(complete)
            self.assertEqual(holders, set())
            self.assertIn("session-identity-unresolved", reasons)

            (process / "cmdline").write_bytes(b"claude\0doctor\0")
            complete, holders, reasons = claude_session_census(proc_root=root)
            self.assertTrue(complete, reasons)
            self.assertEqual(holders, set())

    def test_fake_proc_resume_alias_is_positive_even_when_census_is_incomplete(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "proc"
            home = Path(temp) / "home" / ".claude"
            process = root / "123"
            (process / "ns").mkdir(parents=True)
            (root / "self" / "ns").mkdir(parents=True)
            (home / "sessions").mkdir(parents=True)
            (home / ".statusline").mkdir(parents=True)
            os.symlink("pid:[4026531836]", root / "self" / "ns" / "pid")
            os.symlink("pid:[4026531836]", process / "ns" / "pid")
            os.symlink("/usr/bin/claude", process / "exe")
            (process / "stat").write_text("123 (claude) S " + " ".join(["0"] * 18 + ["42"]))
            (process / "cmdline").write_bytes(b"claude\0--resume\0wanted-sid\0")
            (process / "environ").write_bytes(f"HOME={home.parent}\0".encode())

            complete, holders, _reasons = claude_session_census(proc_root=root)
            self.assertFalse(complete)
            self.assertIn("wanted-sid", holders)

    def test_native_invocation_classifier_separates_commands_from_conversations(self):
        for argv, expected in (
            (["/usr/bin/claude", "daemon"], "session"),
            (["/usr/bin/claude", "remote-control"], "session"),
            (["/usr/bin/claude", "doctor"], "command"),
            (["claude", "mcp", "serve"], "command"),
            (["claude", "--resume", "sid"], "session"),
            (["claude", "-p", "prompt"], "session"),
            (["claude", "--unknown-option"], "unknown"),
            (["node", "/opt/@anthropic-ai/claude-code/cli.js", "daemon"], "session"),
            (["bun", "/opt/@anthropic-ai/claude-code/cli.js", "daemon"], "session"),
        ):
            with self.subTest(argv=argv):
                self.assertEqual(invocation_kind(argv), expected)

    def test_fake_proc_node_and_bun_daemon_launchers_stay_unresolved_in_both_inventories(self):
        for launcher in ("node", "bun"):
            with self.subTest(launcher=launcher), tempfile.TemporaryDirectory() as temp:
                root = Path(temp) / "proc"
                self._fake_proc_root(root)
                proc, home = self._fake_runtime(
                    root, 123, exe=f"/usr/bin/{launcher}",
                    argv=(launcher, "/opt/@anthropic-ai/claude-code/cli.js", "daemon"))

                complete, holders, reasons = claude_session_census(proc_root=root)

                self.assertFalse(complete)
                self.assertEqual(holders, set())
                self.assertIn("session-identity-unresolved", reasons)
                self.assertNotIn("process-inventory-changed", reasons)

                (home / "sessions" / "123.json").write_text(json.dumps({
                    "pid": 123, "procStart": "123", "sessionId": f"{launcher}-daemon-sid"}))
                complete, holders, reasons = claude_session_census(proc_root=root)
                self.assertTrue(complete, reasons)
                self.assertEqual(holders, {f"{launcher}-daemon-sid"})

    def test_fake_proc_native_version_executable_with_claude_argv_is_a_holder(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "proc"
            self._fake_proc_root(root)
            proc, home = self._fake_runtime(root, 123, argv=("claude", "--resume", "resume-sid"))
            (home / "sessions" / "123.json").write_text(json.dumps({
                "pid": 123, "procStart": "123", "sessionId": "native-sid"}))
            complete, holders, reasons = claude_session_census(proc_root=root)
            self.assertTrue(complete, reasons)
            self.assertEqual(holders, {"native-sid", "resume-sid"})

    def test_fake_proc_native_ugrep_helper_is_skipped(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "proc"
            self._fake_proc_root(root)
            self._fake_runtime(root, 123, argv=("ugrep", "--files"))
            complete, holders, reasons = claude_session_census(proc_root=root)
            self.assertTrue(complete, reasons)
            self.assertEqual(holders, set())

    def test_fake_proc_ignores_unreadable_non_claude_executables_by_uid(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "proc"
            self._fake_proc_root(root)
            foreign, _home = self._fake_runtime(root, 123, exe="/usr/sbin/sshd", argv=("sshd",))
            same_uid, _home = self._fake_runtime(root, 124, exe="/usr/sbin/sshd", argv=("sshd",))
            real_stat, real_readlink = Path.stat, os.readlink

            def stat(path, *args, **kwargs):
                value = real_stat(path, *args, **kwargs)
                if path == foreign:
                    return SimpleNamespace(st_uid=os.getuid() + 1)
                return value

            def readlink(path, *args, **kwargs):
                if path in {foreign / "exe", same_uid / "exe"}:
                    raise PermissionError("fixture unreadable exe")
                return real_readlink(path, *args, **kwargs)

            with mock.patch.object(Path, "stat", stat), mock.patch.object(os, "readlink", readlink):
                complete, holders, reasons = claude_session_census(proc_root=root)
            self.assertTrue(complete, reasons)
            self.assertEqual(holders, set())

    def test_fake_proc_other_uid_claude_shaped_cmdline_is_incomplete(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "proc"
            self._fake_proc_root(root)
            proc, _home = self._fake_runtime(root, 123, exe="/usr/bin/node",
                                              argv=("node", "/opt/claude-code/cli.js"))
            real_stat = Path.stat

            def stat(path, *args, **kwargs):
                value = real_stat(path, *args, **kwargs)
                return SimpleNamespace(st_uid=os.getuid() + 1) if path == proc else value

            with mock.patch.object(Path, "stat", stat):
                complete, _holders, reasons = claude_session_census(proc_root=root)
            self.assertFalse(complete)
            self.assertIn("foreign-claude-shaped-process", reasons)

    def test_fake_proc_unknown_argv_on_native_executable_is_incomplete(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "proc"
            self._fake_proc_root(root)
            self._fake_runtime(root, 123, argv=("mystery-launcher", "--x"))
            complete, _holders, reasons = claude_session_census(proc_root=root)
            self.assertFalse(complete)
            self.assertIn("native-process-argv-unresolved", reasons)

    def test_fake_proc_missing_statusline_is_complete_and_multiple_claims_are_holders(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "proc"
            self._fake_proc_root(root)
            proc, home = self._fake_runtime(root, 123)
            (home / "sessions" / "123.json").write_text(json.dumps({
                "pid": 123, "procStart": "123", "sessionId": "native-sid"}))
            complete, holders, reasons = claude_session_census(proc_root=root)
            self.assertTrue(complete, reasons)
            self.assertEqual(holders, {"native-sid"})

            (home / ".statusline").mkdir()
            for sid in ("tap-one", "tap-two"):
                (home / ".statusline" / f"{sid}.json").write_text(json.dumps({
                    "pid": 123, "proc_start": "123", "session_id": sid}))
            complete, holders, reasons = claude_session_census(proc_root=root)
            self.assertTrue(complete, reasons)
            self.assertEqual(holders, {"native-sid", "tap-one", "tap-two"})

    def test_fake_proc_session_without_exact_claim_is_incomplete(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "proc"
            self._fake_proc_root(root)
            self._fake_runtime(root, 123, argv=("claude", "--permission-mode", "bypassPermissions"))
            complete, _holders, reasons = claude_session_census(proc_root=root)
            self.assertFalse(complete)
            self.assertIn("session-identity-unresolved", reasons)


    def _nest(self, proc, *, nspid="NSpid:\t{pid}\t114", pid=123):
        """Move a fake process into a nested PID namespace with an NSpid vector."""
        (proc / "ns" / "pid").unlink()
        os.symlink("pid:[4026534296]", proc / "ns" / "pid")
        if nspid is not None:
            (proc / "status").write_text("Name:\tclaude\n" + nspid.format(pid=pid) + "\n")

    def test_fake_proc_bfs_helper_of_claude_binary_is_skipped_beside_a_real_session(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "proc"
            self._fake_proc_root(root)
            self._fake_runtime(root, 122, argv=("bfs", "/", "-maxdepth", "6"))
            _proc, home = self._fake_runtime(root, 123)
            (home / "sessions" / "123.json").write_text(json.dumps({
                "pid": 123, "procStart": "123", "sessionId": "real-sid"}))
            complete, holders, reasons = claude_session_census(proc_root=root)
            self.assertTrue(complete, reasons)
            self.assertEqual(holders, {"real-sid"})
            self.assertNotIn("process-inventory-changed", reasons)

    def test_fake_proc_nested_namespace_process_binds_by_its_innermost_pid(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "proc"
            self._fake_proc_root(root)
            proc, home = self._fake_runtime(root, 123)
            self._nest(proc)
            (home / "sessions" / "114.json").write_text(json.dumps({
                "pid": 114, "procStart": "123", "sessionId": "nested-sid"}))
            complete, holders, reasons = claude_session_census(proc_root=root)
            self.assertTrue(complete, reasons)
            self.assertEqual(holders, {"nested-sid"})

    def test_fake_proc_nested_namespace_process_binds_by_exact_statusline_tap(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "proc"
            self._fake_proc_root(root)
            proc, home = self._fake_runtime(root, 123)
            self._nest(proc)
            (home / ".statusline").mkdir()
            (home / ".statusline" / "tap-sid.json").write_text(json.dumps({
                "pid": 114, "proc_start": "123", "session_id": "tap-sid"}))
            complete, holders, reasons = claude_session_census(proc_root=root)
            self.assertTrue(complete, reasons)
            self.assertEqual(holders, {"tap-sid"})

    def test_fake_proc_nested_namespace_process_that_does_not_bind_stays_incomplete(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "proc"
            self._fake_proc_root(root)
            proc, home = self._fake_runtime(root, 123)
            self._nest(proc)
            complete, _holders, reasons = claude_session_census(proc_root=root)
            self.assertFalse(complete)
            self.assertIn("session-identity-unresolved", reasons)

            (home / "sessions" / "114.json").write_text(json.dumps({
                "pid": 114, "procStart": "999", "sessionId": "stale-sid"}))
            complete, _holders, reasons = claude_session_census(proc_root=root)
            self.assertFalse(complete)
            self.assertIn("native-session-claim-unusable", reasons)

        for label, nspid in (("missing", None), ("single", "NSpid:\t{pid}"),
                             ("outer-mismatch", "NSpid:\t999\t114"),
                             ("non-numeric", "NSpid:\t{pid}\tx")):
            with self.subTest(label), tempfile.TemporaryDirectory() as temp:
                root = Path(temp) / "proc"
                self._fake_proc_root(root)
                proc, home = self._fake_runtime(root, 123)
                self._nest(proc, nspid=nspid)
                (home / "sessions" / "114.json").write_text(json.dumps({
                    "pid": 114, "procStart": "123", "sessionId": "nested-sid"}))
                complete, holders, reasons = claude_session_census(proc_root=root)
                self.assertFalse(complete)
                self.assertIn("pid-namespace-mismatch", reasons)
                self.assertEqual(holders, set())

    def test_fake_proc_host_namespace_process_still_binds_only_by_host_pid(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "proc"
            self._fake_proc_root(root)
            proc, home = self._fake_runtime(root, 123)
            (proc / "status").write_text("Name:\tclaude\nNSpid:\t123\t114\n")
            (home / "sessions" / "114.json").write_text(json.dumps({
                "pid": 114, "procStart": "123", "sessionId": "inner-sid"}))
            complete, holders, reasons = claude_session_census(proc_root=root)
            self.assertFalse(complete)
            self.assertIn("session-identity-unresolved", reasons)
            self.assertNotIn("inner-sid", holders)

            (home / "sessions" / "123.json").write_text(json.dumps({
                "pid": 123, "procStart": "123", "sessionId": "host-sid"}))
            (home / "sessions" / "114.json").unlink()
            complete, holders, reasons = claude_session_census(proc_root=root)
            self.assertTrue(complete, reasons)
            self.assertEqual(holders, {"host-sid"})

    def test_recipient_census_over_nested_fake_proc_keeps_holder_alive_and_ends_unclaimed_history(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "proc"
            self._fake_proc_root(root)
            proc, home = self._fake_runtime(root, 123)
            self._nest(proc)
            (home / "sessions" / "114.json").write_text(json.dumps({
                "pid": 114, "procStart": "123", "sessionId": "nested-sid"}))
            (home / "history.jsonl").write_text(
                json.dumps({"sessionId": "nested-sid"}) + "\n"
                + json.dumps({"sessionId": "gone-sid"}) + "\n", encoding="utf-8")
            with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(home)}), self._real_census_at(root):
                census = RecipientCensus()
                self.assertEqual(census.state("claude", "nested-sid"), "alive")
                self.assertEqual(census.state("claude", "gone-sid"), "ended")

    def _lineage_process(self, root, number, *, native, taps=(), argv=("claude",), home=None):
        proc, home = self._fake_runtime(root, number, argv=argv, home=home)
        if native:
            (home / "sessions" / f"{number}.json").write_text(json.dumps({
                "pid": number, "procStart": str(number), "sessionId": native}))
        if taps:
            (home / ".statusline").mkdir(exist_ok=True)
            for sid in taps:
                (home / ".statusline" / f"{sid}.json").write_text(json.dumps({
                    "pid": number, "proc_start": str(number), "session_id": sid}))
        return home

    def _incomplete_with_daemon(self, root):
        # An unresolved Claude-shaped process keeps the whole census incomplete.
        self._fake_runtime(root, 999, argv=("claude", "--permission-mode", "bypassPermissions"))

    @staticmethod
    def _chain(mapping):
        return lambda harness, sid: (mapping[sid], "successor") if sid in mapping else (sid, "self")

    def test_same_process_stale_claim_with_confirmed_successor_is_ended_even_if_incomplete(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "proc"
            self._fake_proc_root(root)
            self._lineage_process(root, 123, native="sid-new", taps=("sid-old",),
                                  argv=("claude", "--resume", "sid-old"))
            self._incomplete_with_daemon(root)
            complete, holders, _reasons = claude_session_census(proc_root=root)
            self.assertFalse(complete)
            self.assertEqual(holders, {"sid-old", "sid-new"})
            with self._real_census_at(root):
                census = RecipientCensus(lineage=self._chain({"sid-old": "sid-new"}))
                self.assertEqual(census.state("claude", "sid-old"), "ended")
                self.assertEqual(census.state("claude", "sid-new"), "alive")
                # ii: without the lineage the stale claim stays a live holder.
                census = RecipientCensus(lineage=self._chain({}))
                self.assertEqual(census.state("claude", "sid-old"), "alive")

    def test_stale_claim_that_is_not_the_native_sessions_predecessor_stays_alive(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "proc"
            self._fake_proc_root(root)
            # iii: native registration lags: native is the old session, the tap is the new one.
            self._lineage_process(root, 123, native="sid-old", taps=("sid-new",))
            with self._real_census_at(root):
                census = RecipientCensus(lineage=self._chain({"sid-old": "sid-new"}))
                self.assertEqual(census.state("claude", "sid-new"), "alive")
                self.assertEqual(census.state("claude", "sid-old"), "alive")

    def test_stale_claim_must_resolve_to_every_naming_process_native(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "proc"
            self._fake_proc_root(root)
            # iv-1: the lineage ends at a session other than the process's native.
            self._lineage_process(root, 123, native="sid-new", taps=("sid-old",))
            with self._real_census_at(root):
                census = RecipientCensus(lineage=self._chain({"sid-old": "sid-other"}))
                self.assertEqual(census.state("claude", "sid-old"), "alive")
                unresolved = RecipientCensus(lineage=lambda _h, sid: (sid, "unresolved"))
                self.assertEqual(unresolved.state("claude", "sid-old"), "alive")
                broken = RecipientCensus(lineage=lambda *_a: (_ for _ in ()).throw(OSError("x")))
                self.assertEqual(broken.state("claude", "sid-old"), "alive")
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "proc"
            self._fake_proc_root(root)
            # iv-2: a second process names the same SID without a native session.
            self._lineage_process(root, 123, native="sid-new", taps=("sid-old",))
            self._lineage_process(root, 124, native=None, argv=("claude", "--resume", "sid-old"))
            with self._real_census_at(root):
                census = RecipientCensus(lineage=self._chain({"sid-old": "sid-new"}))
                self.assertEqual(census.state("claude", "sid-old"), "alive")
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "proc"
            self._fake_proc_root(root)
            # iv-3: two processes carry the SID in a tap; only one native matches the lineage end.
            self._lineage_process(root, 123, native="sid-new", taps=("sid-old",))
            self._lineage_process(root, 124, native="sid-m", taps=("sid-old",))
            with self._real_census_at(root):
                census = RecipientCensus(lineage=self._chain({"sid-old": "sid-new"}))
                self.assertEqual(census.state("claude", "sid-old"), "alive")

    def test_plain_set_holders_never_use_lineage(self):
        with mock.patch.object(claude_process_identity, "claude_session_census",
                               return_value=(False, {"sid-old"}, {"x"})):
            census = RecipientCensus(lineage=self._chain({"sid-old": "sid-new"}))
            self.assertEqual(census.state("claude", "sid-old"), "alive")


if __name__ == "__main__":
    unittest.main()
