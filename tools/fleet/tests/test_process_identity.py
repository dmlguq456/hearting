"""A process's harness session, in the one identity record shape (OpenCode included)."""
import json
from pathlib import Path
import os
import shutil
import sys
import subprocess
import tempfile
import unittest
import unittest.mock
from contextlib import ExitStack
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[3]
for entry in (str(ROOT / "tools"), str(ROOT / "utilities")):
    if entry not in sys.path:
        sys.path.insert(0, entry)

from fleet import herdr_projection as hp  # noqa: E402
from fleet import process_identity as P  # noqa: E402
from fleet.collectors import opencode  # noqa: E402
from fleet.collectors import claude  # noqa: E402
import claude_process_identity as claude_identity  # noqa: E402

SID = "ses_abcDEF123"


class FakeClaudeProc:
    """Private proc/native files; readers and classification remain production code."""
    def __init__(self, root):
        self.root = Path(root) / "proc"
        self.home = Path(root) / "claude"
        self.root.mkdir()
        (self.home / "sessions").mkdir(parents=True)

    def process(self, pid=71, parent=1, start="101", argv=None, executable=None):
        proc = self.root / str(pid)
        proc.mkdir(exist_ok=True)
        (proc / "stat").write_text(f"{pid} (native fixture) S {parent} " +
                                   " ".join(["0"] * 17 + [start]))
        (proc / "cmdline").write_bytes(b"\0".join(os.fsencode(x) for x in
            (argv or ["claude"])) + b"\0")
        (proc / "exe").unlink(missing_ok=True)
        (proc / "exe").symlink_to(executable or "/opt/claude/versions/8.7.6")
        return proc

    def registry(self, pid=71, start="101", sid="current", **extra):
        path = self.home / "sessions" / f"{pid}.json"
        path.write_text(json.dumps({"pid": pid, "procStart": start, "sessionId": sid, **extra}))
        return path

    def tap(self, sid="current", pid=71, start="101"):
        directory = self.home / ".statusline"
        directory.mkdir(exist_ok=True)
        (directory / f"{sid}.json").write_text(json.dumps({
            "session_id": sid, "pid": pid, "proc_start": start}))


class ClaudeNativeIdentityHelperTest(unittest.TestCase):
    def test_focused_native_predicate_rejects_appearance_and_prompt_false_positives(self):
        cases = [
            ("/opt/claude/versions/8.7.6", ["claude"], "runtime"),
            ("/usr/bin/claude", ["claude", "--model", "daemon"], "runtime"),
            ("/opt/claude/versions/8.7.6", ["claude", "--session-id", "new",
                "--fork-session", "--resume", "/old/transcript.jsonl"], "runtime"),
            ("/usr/bin/node", ["node", "/opt/@anthropic-ai/claude-code/cli.js"], "runtime"),
            ("/usr/bin/bun", ["bun", "/opt/@anthropic-ai/claude-code/cli.js", "daemon"], "service"),
            ("/usr/bin/claude", ["claude", "daemon", "run"], "service"),
            ("/opt/claude/versions/8.7.6", ["claude bg-pty-host", "--bg-pty-host", "/sock"], "service"),
            ("/opt/claude/versions/8.7.6", ["claude bg-spare"], "service"),
            ("/opt/claude/versions/8.7.6", ["claude", "--bg-pty-host", "/sock"], "service"),
            ("/usr/bin/claude", ["claude", "-p", "daemon"], "runtime"),
            ("/usr/bin/claude", ["claude", "--system-prompt", "daemon"], "runtime"),
            ("/usr/bin/claude", ["claude", "--unknown", "daemon"], "unknown"),
            ("/opt/claude/versions/8.7.6", ["rg", "claude"], "helper"),
            ("/usr/bin/python3", ["python3", "/tmp/claude", "--resume", "sid"], "helper"),
            ("/usr/bin/node", ["node", "/tmp/app.js", "@anthropic-ai/claude-code"], "helper"),
            ("/tmp/8.7.6", ["8.7.6", "claude"], "helper"),
        ]
        with tempfile.TemporaryDirectory() as temporary:
            fixture = FakeClaudeProc(temporary)
            for executable, argv, expected in cases:
                with self.subTest(executable=executable, argv=argv):
                    fixture.process(executable=executable, argv=argv)
                    observed = claude_identity.observe_process(71, proc_root=fixture.root)
                    self.assertEqual(observed["kind"], expected)
                    self.assertEqual((observed["pid"], observed["proc_start"]), (71, "101"))
            with unittest.mock.patch.object(claude_identity.os, "geteuid", return_value=os.getuid() + 1):
                self.assertIsNone(claude_identity.observe_process(71, proc_root=fixture.root))

    def test_nearest_runtime_binding_and_registry_loss_fallback(self):
        with tempfile.TemporaryDirectory() as temporary:
            f = FakeClaudeProc(temporary)
            f.process(73, parent=72, executable="/bin/bash", argv=["bash", "statusline.sh"])
            f.process(72, parent=71, start="102")
            f.process(71)
            f.registry(71)
            lookup = lambda: claude_identity.statusline_identity(73, "current", f.home, proc_root=f.root)
            self.assertIsNone(lookup(), "must not borrow the upper runtime's registry")
            path = f.registry(72, start="102", sid="foreign")
            self.assertIsNone(lookup())
            f.registry(72, start="102", sid="current")
            self.assertEqual(lookup()["pid"], 72)
            f.registry(72, start="stale", sid="current")
            self.assertIsNone(lookup())
            path.write_text("{")
            self.assertIsNone(lookup())
            path.unlink()
            f.tap(pid=72, start="102")
            self.assertEqual(lookup()["pid"], 72)
            f.tap(sid="conflict", pid=72, start="102")
            self.assertIsNone(lookup())
            # An exact current native record outranks historical launch/tap claims.
            f.registry(72, start="102")
            self.assertEqual(lookup()["pid"], 72)

    def test_focused_observation_revalidates_process_reads_and_binding(self):
        with tempfile.TemporaryDirectory() as temporary:
            f = FakeClaudeProc(temporary)
            proc = f.process()
            real_readlink = os.readlink
            def reuse(path):
                result = real_readlink(path)
                f.process(start="102")
                return result
            with unittest.mock.patch.object(claude_identity.os, "readlink", side_effect=reuse):
                self.assertIsNone(claude_identity.observe_process(71, proc_root=f.root))
            f.process()
            f.registry()
            original_claim = claude_identity._native_session_observation
            def exit_after_claim(*args):
                result = original_claim(*args)
                (proc / "cmdline").unlink()
                return result
            with unittest.mock.patch.object(claude_identity, "_native_session_observation", side_effect=exit_after_claim):
                self.assertIsNone(claude_identity.statusline_identity(71, "current", f.home, proc_root=f.root))
            with unittest.mock.patch.object(claude_identity.Path, "open", side_effect=PermissionError()):
                self.assertIsNone(claude_identity.observe_process(71, proc_root=f.root))

    def test_explicit_current_argument_and_unreadable_registry_never_borrow_parent(self):
        with tempfile.TemporaryDirectory() as temporary:
            f = FakeClaudeProc(temporary)
            f.process(71, argv=["claude", "daemon", "run"])
            f.registry(71)
            for argv, bound in ((["claude", "--session-id", "current", "--fork-session",
                                  "--resume", "/old.jsonl"], True),
                                (["claude", "--session-id=foreign"], False),
                                (["claude", "--resume", "current"], False),
                                (["claude", "--model", "--session-id=current"], False),
                                (["claude", "--unknown", "--session-id=current"], False),
                                (["claude", "--bg-pty-host=/sock"], False)):
                with self.subTest(argv=argv):
                    f.process(72, parent=71, argv=argv)
                    self.assertEqual(bool(claude_identity.statusline_identity(
                        72, "current", f.home, proc_root=f.root)), bound)
            f.process(72, parent=71)
            f.registry(72)
            with unittest.mock.patch.object(claude_identity.os, "open", side_effect=PermissionError()):
                self.assertIsNone(claude_identity.statusline_identity(72, "current", f.home, proc_root=f.root))

    def test_indexed_and_direct_taps_match_fleet_exact_pid_start_claims(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            taps = home / ".statusline"
            taps.mkdir()
            (taps / "one.json").write_text(json.dumps({
                "pid": 17, "proc_start": "100", "session_id": "sid-one"}))
            (taps / "two.json").write_text(json.dumps({
                "pid": "17", "proc_start": "100", "session_id": "sid-two"}))
            (taps / "stale.json").write_text(json.dumps({
                "pid": 17, "proc_start": "99", "session_id": "sid-stale"}))
            (taps / "bad.json").write_text("{")

            direct = claude_identity.tap_sids_by_pid(home, 17, "100")
            indexed = claude_identity.tap_sids_by_pid(
                home, 17, "100", index=claude_identity.tap_claim_index(home))
            self.assertEqual(direct, {"sid-one", "sid-two"})
            self.assertEqual(indexed, direct)
            self.assertEqual(claude._tap_sids_by_pid(home, 17, "100"), direct)
            self.assertEqual(claude_identity.tap_sids_by_pid(home, 17, "101"), set())

    def test_strict_census_tap_index_surfaces_unreadable_identity_evidence(self):
        with tempfile.TemporaryDirectory() as temp:
            taps = Path(temp) / ".statusline"
            taps.mkdir()
            (taps / "bad.json").write_text("{")
            with self.assertRaises(OSError):
                claude_identity.tap_claim_index(temp, strict=True)

    def test_prior_exact_taps_precede_historical_launch_selectors(self):
        for selector, taps, expected in (
                ("current", ["foreign"], False),
                ("old", ["current"], True),
                ("current", ["current", "foreign"], False),
                ("current", [], True),  # first event, missing directory
                ("foreign", [], False)):
            with self.subTest(selector=selector, taps=taps), tempfile.TemporaryDirectory() as tmp:
                f = FakeClaudeProc(tmp)
                f.process(argv=["claude", "--session-id", selector])
                for sid in taps:
                    f.tap(sid=sid)
                result = claude_identity.statusline_identity(71, "current", f.home, proc_root=f.root)
                self.assertEqual(bool(result), expected)

    def test_launch_fallback_requires_complete_readable_inventory(self):
        for inventory in ("empty", "malformed", "unreadable", "incomplete", "not-directory"):
            with self.subTest(inventory=inventory), tempfile.TemporaryDirectory() as tmp:
                f = FakeClaudeProc(tmp)
                f.process(argv=["claude", "--session-id=current"])
                directory = f.home / ".statusline"
                if inventory == "not-directory":
                    directory.write_text("not a directory")
                else:
                    directory.mkdir()
                if inventory == "malformed":
                    (directory / "bad.json").write_text("{")
                elif inventory == "incomplete":
                    for index in range(129):
                        (directory / f"{index}.json").write_text("{}")
                patch = (unittest.mock.patch.object(claude_identity.os, "scandir", side_effect=PermissionError())
                         if inventory == "unreadable" else unittest.mock.patch.dict(os.environ, {}))
                with patch:
                    lookup = lambda: claude_identity.statusline_identity(71, "current", f.home, proc_root=f.root)
                    self.assertEqual(bool(lookup()), inventory == "empty")
                    # Exact current native selection is stronger even when taps
                    # cannot be inventoried or the launch SID is historical.
                    f.process(argv=["claude", "--session-id=old"])
                    f.registry()
                    self.assertEqual(lookup()["pid"], 71)
                    f.registry(sid="foreign")
                    self.assertIsNone(lookup())

    def test_unknown_nearest_native_stops_but_known_embedded_helpers_allow_parent_lookup(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = FakeClaudeProc(tmp)
            f.process(71)
            f.registry(71)
            f.registry(72, start="102", sid="foreign")
            for argv0, expected in (("unrecognized-runtime-title", None),
                                    ("rg", 71), ("ugrep", 71), ("bfs", 71)):
                with self.subTest(argv0=argv0):
                    f.process(72, parent=71, start="102", argv=[argv0])
                    observation = claude_identity.observe_process(72, proc_root=f.root)
                    self.assertEqual(observation["kind"], "unknown" if expected is None else "helper")
                    result = claude_identity.statusline_identity(72, "current", f.home, proc_root=f.root)
                    self.assertEqual(result["pid"] if result else None, expected)

    def test_retained_claim_requires_fresh_owner_runtime_and_readable_exact_prior(self):
        for case in ("conflict", "incomplete", "unreadable-inventory", "foreign-runtime",
                     "foreign-tap", "unreadable-prior", "oversize-prior", "denied-prior",
                     "replaced-prior", "reuse", "exit"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
                f = FakeClaudeProc(tmp)
                f.process()
                f.tap()
                f.tap(sid="old")
                if case == "incomplete":
                    for index in range(129):
                        (f.home / ".statusline" / f"extra-{index}.json").write_text("{}")
                elif case == "unreadable-inventory":
                    stack.enter_context(unittest.mock.patch.object(claude_identity.os, "scandir",
                                                                   side_effect=PermissionError()))
                elif case in {"foreign-runtime", "foreign-tap"}:
                    name = "geteuid" if case == "foreign-runtime" else "getuid"
                    stack.enter_context(unittest.mock.patch.object(claude_identity.os, name,
                                                                   return_value=os.getuid() + 1))
                elif case == "unreadable-prior":
                    (f.home / ".statusline/current.json").write_text("{")
                elif case == "oversize-prior":
                    path = f.home / ".statusline/current.json"
                    row = json.loads(path.read_text())
                    path.write_text(json.dumps({**row, "padding": "x" * 65536}))
                elif case == "denied-prior":
                    real_claim = claude_identity._focused_tap_claim
                    def denied_claim(path, pid, start):
                        if path.name == "current.json":
                            raise PermissionError("private exact-prior read denied")
                        return real_claim(path, pid, start)
                    stack.enter_context(unittest.mock.patch.object(claude_identity, "_focused_tap_claim",
                                                                   side_effect=denied_claim))
                else:
                    real_claim = claude_identity._focused_tap_claim
                    def changed_claim(path, pid, start):
                        result = real_claim(path, pid, start)
                        if path.name == "current.json":
                            if case == "reuse":
                                f.process(start="102")
                            elif case == "exit":
                                (f.root / "71/cmdline").unlink(missing_ok=True)
                        return result
                    if case in {"reuse", "exit"}:
                        stack.enter_context(unittest.mock.patch.object(claude_identity, "_focused_tap_claim",
                                                                       side_effect=changed_claim))
                    elif case == "replaced-prior":
                        path = f.home / ".statusline/current.json"
                        replacement = path.with_suffix(".new")
                        real_read = claude_identity.os.read
                        def replace_during_read(fd, size):
                            reading_prior = os.fstat(fd).st_ino == path.stat().st_ino
                            raw = real_read(fd, size)
                            if reading_prior:
                                replacement.write_bytes(raw)
                                os.replace(replacement, path)
                            return raw
                        stack.enter_context(unittest.mock.patch.object(claude_identity.os, "read",
                                                                       side_effect=replace_during_read))
                current, tap, policy = claude_identity.statusline_bindings(71, "current", f.home, proc_root=f.root)
                self.assertIsNone(current)
                self.assertEqual(tap["pid"] if tap else None,
                                 71 if case in {"conflict", "incomplete", "unreadable-inventory"} else None)
                self.assertEqual(policy, "unobserved" if case in {
                    "unreadable-prior", "oversize-prior", "denied-prior", "replaced-prior",
                    "reuse", "exit"} else "replace")

    def test_native_read_failures_are_unknown_but_raw_reader_stays_unusable(self):
        for kind in ("runtime", "unknown"):
            for fault in ("denied", "oversize", "replaced", "short-read"):
                with self.subTest(kind=kind, fault=fault), tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
                    f = FakeClaudeProc(tmp)
                    f.process(argv=["claude"] if kind == "runtime" else ["unrecognized-runtime-title"])
                    path = f.registry()
                    f.tap()
                    f.tap(sid="old")
                    if fault == "oversize":
                        row = json.loads(path.read_text())
                        path.write_text(json.dumps({**row, "padding": "x" * 65536}))
                    elif fault == "denied":
                        real_open = os.open
                        def denied_open(candidate, *args, **kwargs):
                            if Path(candidate) == path:
                                raise PermissionError("private native read denied")
                            return real_open(candidate, *args, **kwargs)
                        stack.enter_context(unittest.mock.patch.object(claude_identity.os, "open",
                                                                       side_effect=denied_open))
                    else:
                        real_read = os.read
                        def interrupted_read(fd, size):
                            native = os.fstat(fd).st_ino == path.stat().st_ino
                            raw = real_read(fd, size)
                            if native:
                                if fault == "short-read":
                                    return raw[:len(raw) // 2]
                                replacement = path.with_suffix(".new")
                                replacement.write_bytes(raw)
                                os.replace(replacement, path)
                            return raw
                        stack.enter_context(unittest.mock.patch.object(claude_identity.os, "read",
                                                                       side_effect=interrupted_read))
                    self.assertEqual(claude_identity._native_session_claim(f.home, "71", "101"), (None, False))
                    self.assertEqual(claude_identity.statusline_bindings(
                        71, "current", f.home, proc_root=f.root), (None, None, "unobserved"))

    def test_foreign_prior_cannot_enable_launch_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = FakeClaudeProc(tmp)
            f.process(argv=["claude", "--session-id=current"])
            f.tap()
            with unittest.mock.patch.object(claude_identity.os, "getuid", return_value=os.getuid() + 1):
                self.assertEqual(claude_identity.statusline_bindings(
                    71, "current", f.home, proc_root=f.root), (None, None, "replace"))


class OpenCodeProcessTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.env = {"XDG_STATE_HOME": self._tmp.name}
        self.pid = os.getpid()
        self.start = opencode._proc_start_ticks(self.pid)
        self.dir = Path(self._tmp.name) / "hearting" / "tui-identity"
        self.dir.mkdir(parents=True)

    def record(self, **overrides):
        row = {"schema": "hearting-tui-selection-v1", "sessionID": SID, "pid": self.pid,
               "start": self.start, "writtenAt": "2026-10-07T00:00:00Z", **overrides}
        (self.dir / f"{self.pid}-{self.start}.json").write_text(json.dumps(row))

    def test_the_tui_selection_record_names_the_pane_session(self):
        self.assertEqual(opencode.session_of_process(self.pid, self.env), (None, ""))
        self.record()
        self.assertEqual(opencode.session_of_process(self.pid, self.env), (SID, "opencode-tui-selection"))
        for bad in ({"start": "1"}, {"pid": self.pid + 1}, {"schema": "other"}, {"sessionID": "not-a-session"}):
            with self.subTest(bad=bad):
                self.record(**bad)
                self.assertEqual(opencode.session_of_process(self.pid, self.env), (None, ""))
        (self.dir / f"{self.pid}-{self.start}.json").write_text(" " * 2000)
        self.assertEqual(opencode.session_of_process(self.pid, self.env), (None, ""))

    def test_current_selection_overrides_the_session_the_process_started_on(self):
        self.record()
        with unittest.mock.patch.object(
                opencode, "_cmdline", return_value=["opencode", "--session", "ses_stale"]):
            self.assertEqual(opencode.session_of_process(self.pid, self.env),
                             (SID, "opencode-tui-selection"))

    def test_the_started_session_argument(self):
        cases = ((["opencode", "--session", SID], SID), (["opencode", f"--session={SID}"], SID),
                 (["/usr/bin/opencode", "-s", SID, "--model", "x"], SID), (["opencode"], None),
                 (["opencode", "run", "--session", SID], None), (["opencode", "--session", "bad"], None),
                 (["opencode", "-s", SID, "-s", SID], None), (["node", "--session", SID], None))
        for argv, expected in cases:
            with self.subTest(argv=argv):
                self.assertEqual(opencode._argv_session(argv), expected)

    def test_retained_reader_uses_only_the_original_lifetime_and_keeps_legacy_mode(self):
        self.record()
        path = self.dir / f"{self.pid}-{self.start}.json"
        path.chmod(0o664)
        with unittest.mock.patch.object(opencode, "_proc_start_ticks", return_value=None):
            self.assertEqual(opencode.session_of_process(self.pid, self.env), (None, ""))
            record = opencode._tui_selection_record(self.pid, self.start, self.env)
        self.assertEqual(record["value"]["sessionID"], SID)
        self.assertGreater(record["source"]["written_at"], 0)
        self.assertEqual(record["source"]["mtime"], path.stat().st_mtime)
        for pid, start in ((True, self.start), (0, self.start), (self.pid, True),
                           (self.pid, ""), (self.pid, "../x"), (self.pid, "0"),
                           (self.pid, str(int(self.start) + 1))):
            with self.subTest(pid=pid, start=start):
                self.assertIsNone(opencode._tui_selection_record(pid, start, self.env))

    def test_native_record_types_paths_and_provenance_refuse_unknown(self):
        path = self.dir / f"{self.pid}-{self.start}.json"
        for value in (False, None, [], {}, {"schema": "hearting-tui-selection-v1",
                "pid": self.pid, "start": self.start, "sessionID": [SID]},
                {"schema": "hearting-tui-selection-v1", "pid": True,
                 "start": self.start, "sessionID": SID}):
            with self.subTest(value=value):
                path.write_text(json.dumps(value))
                self.assertIsNone(opencode._tui_selection_record(self.pid, self.start, self.env))
        for raw in (b"{", b"\xff", b" " * 1025):
            path.write_bytes(raw)
            self.assertIsNone(opencode._tui_selection_record(self.pid, self.start, self.env))
        path.unlink()
        path.symlink_to(self.dir / "absent")
        self.assertIsNone(opencode._tui_selection_record(self.pid, self.start, self.env))
        path.unlink()
        os.mkfifo(path)
        self.assertIsNone(opencode._tui_selection_record(self.pid, self.start, self.env))
        path.unlink()
        self.record()
        uid = os.getuid()
        with unittest.mock.patch.object(opencode.os, "getuid", return_value=uid + 1):
            self.assertIsNone(opencode._tui_selection_record(self.pid, self.start, self.env))
        with unittest.mock.patch.object(opencode.os, "open", side_effect=PermissionError()):
            self.assertIsNone(opencode._tui_selection_record(self.pid, self.start, self.env))

    def test_atomic_replacement_during_read_is_unknown_even_with_identical_bytes(self):
        self.record()
        path = self.dir / f"{self.pid}-{self.start}.json"
        real = os.fstat
        calls = 0
        def replace(fd):
            nonlocal calls
            calls += 1
            result = real(fd)
            if calls == 2:
                other = path.with_suffix(".new")
                other.write_bytes(path.read_bytes())
                os.replace(other, path)
            return result
        with unittest.mock.patch.object(opencode.os, "fstat", side_effect=replace):
            self.assertIsNone(opencode._tui_selection_record(self.pid, self.start, self.env))

    def test_written_at_is_optional_for_live_identity_but_not_retained_provenance(self):
        for written in (None, "yesterday", "2026-10-07T00:00:00", False):
            with self.subTest(written=written):
                self.record(writtenAt=written)
                record = opencode._tui_selection_record(self.pid, self.start, self.env)
                self.assertIsNone(record["source"]["written_at"])
                self.assertEqual(opencode.session_of_process(self.pid, self.env), (SID, "opencode-tui-selection"))

    def test_one_record_shape_for_every_harness(self):
        self.record()
        with unittest.mock.patch.dict(os.environ, self.env):
            found = P.process_identity(self.pid, "opencode")
        self.assertEqual((found.harness, found.session_id, found.source, found.confidence),
                         ("opencode", SID, "opencode-tui-selection", P.PROVEN))
        with unittest.mock.patch("fleet.collectors.claude.session_id_of_process", return_value="c-1"):
            found = P.process_identity(self.pid, "claude")
        self.assertEqual((found.harness, found.session_id, found.confidence), ("claude", "c-1", P.PROVEN))
        observation = object()
        with unittest.mock.patch("fleet.collectors.codex.session_id_of_process",
                                 return_value="c-1") as codex_identity:
            found = P.process_identity(self.pid, "codex", codex_observation=observation)
        codex_identity.assert_called_once_with(self.pid, None, observation=observation)
        self.assertEqual((found.harness, found.session_id, found.confidence),
                         ("codex", "c-1", P.PROVEN))
        with unittest.mock.patch("fleet.collectors.codex.session_id_of_process", return_value=None):
            found = P.process_identity(self.pid, "codex")
        self.assertEqual((found.harness, found.session_id, found.confidence), ("codex", "", P.HARNESS_ONLY))


class OpenCodeTuiProducerBridgeTest(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node.js is required for the TUI identity bridge")
    def test_v1_and_v2_producers_write_the_common_reader_contract(self):
        source = ROOT / "adapters/opencode/tui/hearting-tui-identity.ts"
        script = r'''import { pathToFileURL } from "node:url";
const plugin = await import(pathToFileURL(process.env.HEARTING_PLUGIN));
const sid = "ses_nativeBridge123";
let dispose;
const events = { on: () => () => {} };
const lifecycle = { onDispose: (fn) => { dispose = fn; } };
const api = process.env.HEARTING_API === "v2"
  ? { ui: { router: { current: () => ({type: "session", sessionID: sid}) } },
      event: events, lifecycle }
  : { route: { current: {name: "session", params: {sessionID: sid}} },
      event: events, lifecycle, slots: {register: () => {}} };
if (process.env.HEARTING_API === "v2") plugin.default.setup(api);
else await plugin.default.tui(api);
const stat = (await import("node:fs")).readFileSync("/proc/self/stat", "utf8");
const start = stat.slice(stat.lastIndexOf(") ") + 2).trim().split(/\s+/)[19];
process.stdout.write(JSON.stringify({pid: process.pid, start}) + "\n");
setInterval(() => {}, 1000);
'''
        with tempfile.TemporaryDirectory(prefix="opencode-identity-bridge-") as temporary:
            root = Path(temporary)
            for api in ("v1", "v2"):
                entry = root / (api + ".mjs")
                shutil.copyfile(source, entry)
                state = root / ("state-" + api)
                state.mkdir()
                process = subprocess.Popen(
                    [shutil.which("node"), "--input-type=module", "-e", script],
                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True, env={**os.environ, "XDG_STATE_HOME": str(state),
                                    "HEARTING_PLUGIN": str(entry), "HEARTING_API": api},
                )
                try:
                    identity = json.loads(process.stdout.readline())
                    self.assertEqual(
                        opencode.session_of_process(identity["pid"],
                                                    {"XDG_STATE_HOME": str(state)}),
                        ("ses_nativeBridge123", "opencode-tui-selection"),
                        api,
                    )
                finally:
                    process.terminate()
                    process.communicate(timeout=5)


class StartedOnIsNotProofTest(unittest.TestCase):
    def test_a_start_time_session_argument_is_reported_but_never_proof(self):
        # A pane started on X can switch to Y inside the TUI; only the selection record follows.
        with unittest.mock.patch.object(opencode, "session_of_process", return_value=(SID, "opencode-argv")):
            found = P.process_identity(os.getpid(), "opencode")
            self.assertEqual((found.session_id, found.confidence), (SID, P.STARTED_ON))
            with unittest.mock.patch.object(hp, "_comm", return_value="opencode"), \
                    unittest.mock.patch("fleet.collectors.claude.session_id_of_process", return_value=None), \
                    unittest.mock.patch.object(hp, "verified_pane", return_value="fixture-pane"):
                self.assertEqual(hp.runtime_identity(), ("opencode", None))
                self.assertTrue(hp.may_report("opencode", "ses_switched", worker=False))


class OpenCodeReportTest(unittest.TestCase):
    def test_a_proven_opencode_session_must_match_and_an_unproven_one_keeps_the_harness_check(self):
        # Native SID policy on an admitted pane; real ownership is exercised in
        # utilities/pane_ownership.test.py on separate foreground/background ptys.
        with unittest.mock.patch.object(hp, "runtime_identity", return_value=("opencode", SID)), \
                unittest.mock.patch.object(hp, "verified_pane", return_value="fixture-pane"):
            self.assertTrue(hp.may_report("opencode", SID, worker=False))
            self.assertFalse(hp.may_report("opencode", "ses_other", worker=False))
        with unittest.mock.patch.object(hp, "runtime_identity", return_value=("opencode", None)), \
                unittest.mock.patch.object(hp, "verified_pane", return_value="fixture-pane"):
            self.assertTrue(hp.may_report("opencode", "ses_other", worker=False))


class LaunchContextTest(unittest.TestCase):
    def test_argv_and_cwd_are_returned_only_for_a_stable_proven_session_birth(self):
        found = SimpleNamespace(confidence=P.PROVEN, session_id=SID)
        argv = ["/opt/opencode/bin/opencode", "--resume", SID, "--model", "fixture"]
        with unittest.mock.patch.object(P, "_proc_stat", side_effect=[(1, "123"), (1, "123")]), \
                unittest.mock.patch.object(P, "process_identity", return_value=found), \
                unittest.mock.patch.object(P, "_proc_argv", return_value=argv), \
                unittest.mock.patch.object(P.os, "readlink", return_value="/sandbox/project"):
            context = P.launch_context(4321, "opencode", SID)
        self.assertEqual(context, {"pid": 4321, "proc_start": "123", "harness": "opencode",
                                  "session_id": SID, "argv": argv,
                                  "cwd": "/sandbox/project"})

    def test_pid_reuse_or_session_change_discards_launcher_context(self):
        found = SimpleNamespace(confidence=P.PROVEN, session_id=SID)
        with unittest.mock.patch.object(P, "_proc_stat", side_effect=[(1, "123"), (1, "124")]), \
                unittest.mock.patch.object(P, "process_identity", return_value=found), \
                unittest.mock.patch.object(P, "_proc_argv", return_value=["opencode", "--session", SID]), \
                unittest.mock.patch.object(P.os, "readlink", return_value="/sandbox/project"):
            self.assertIsNone(P.launch_context(4321, "opencode", SID))


class NativeResumeArgvTest(unittest.TestCase):
    def test_shared_case_table_rekeys_launches_and_preserves_raw_input(self):
        cases = json.loads((Path(__file__).with_name("native_resume_cases.json")).read_text())
        for case in cases:
            argv = list(case["argv"])
            with self.subTest(name=case["name"]):
                self.assertEqual(P.native_resume_wire_argv(argv, case["harness"], case["sid"]),
                                 case["expected"])
                self.assertEqual(argv, case["argv"])
                self.assertEqual(P.is_native_resume_argv(argv, case["harness"], case["sid"]),
                                 case["expected"] is not None)

    def test_bad_executables_foreign_wrappers_wrong_sid_and_bounded_inputs_are_rejected(self):
        cases = (
            (["/usr/bin/python3", "/opt/claude", "--resume", SID], "claude", SID),
            (["/usr/bin/env", "claude", "--resume", SID], "claude", SID),
            (["bin/claude", "--resume", SID], "claude", SID),
            (["/opt/claude", "--resume", SID, "\0bad"], "claude", SID),
            (["/opt/claude", "--resume", SID, "x" * 8192], "claude", SID),
            (["/opt/claude", *(["x"] * 63), SID], "claude", SID),
            (["/opt/claude", "--resume", SID], "codex", SID),
            (["claude"], "claude", ""),
        )
        for argv, harness, sid in cases:
            with self.subTest(argv=argv[:3], harness=harness, sid=sid):
                self.assertFalse(P.is_native_resume_argv(argv, harness, sid))
                self.assertIsNone(P.native_resume_wire_argv(argv, harness, sid))
        self.assertIsNone(P.native_resume_wire_argv(["claude", "--fork-session"],
                                                    "claude", "x" * 8192))


if __name__ == "__main__":
    unittest.main()
